#!/usr/bin/env python3
"""Read-only, aggregate inventory for a separately approved Live transition.

No app imports, migrations, secret reads, names, passwords or write mode.
Run on the host/container with access to the event data directory.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def value(db: sqlite3.Connection, key: str, default=None):
    row = db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def inspect_database(path: Path, *, authority: bool = False) -> dict:
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schema = value(db, 'schema_version')
        if schema != 14:
            raise ValueError('Review this inventory against the deployed schema before proceeding.')
        counted = ('roster', 'roster_members', 'team_assignments', 'sheets', 'participant_teams',
                   'judge_invites', 'registration_links', 'audit', 'visitor_activity',
                   'notifications', 'assistant_threads', 'assistant_jobs', 'assistant_proposals',
                   'agent_receipts', 'agent_confirmations', 'agent_audit')
        result = {'schema': schema, 'integrity': db.execute('PRAGMA quick_check').fetchone()[0],
                  'rows': {t: db.execute('SELECT COUNT(*) FROM "' + t + '"').fetchone()[0]
                           for t in counted if t in tables}}
        # Include legacy first/second slots exactly as the application does.
        people = db.execute("SELECT SUM(CASE WHEN trim(coalesce(p1,''))!='' THEN 1 ELSE 0 END + "
                            "CASE WHEN trim(coalesce(p2,''))!='' THEN 1 ELSE 0 END) FROM roster").fetchone()[0] or 0
        people += db.execute("SELECT COUNT(*) FROM roster_members m JOIN roster r ON r.pair=m.pair "
                             "WHERE m.member>2 AND trim(m.name)!=''").fetchone()[0]
        result['named_participants'] = people
        result['configuration'] = value(db, 'event_configuration', {})
        result['selected_workspace'] = value(db, 'workspace_control', {}).get('mode') if authority else None
        quiz = value(db, 'event_quiz_v1', {})
        result['quiz'] = {k: len(quiz.get(k, {})) for k in ('edits', 'runs', 'members', 'answers', 'delays')}
        result['quiz']['respondents'] = result['quiz'].pop('answers')
        result['quiz']['answers'] = sum(len(answers) for answers in quiz.get('answers', {}).values())
        result['quiz'].update(enabled=bool(quiz.get('enabled')), hall_active=bool(quiz.get('hall')),
                              reset_archives=len(value(db, 'event_quiz_reset_archive_v1', [])),
                              definition_digest=digest({k: quiz.get(k) for k in ('edits', 'prize')}))
        packs = value(db, 'audience_questions_v1', {}).get('packs', [])
        result['audience'] = {'packs': len(packs), 'stored_statuses': dict(Counter(p['status'] for p in packs))}
        prefixes = ('audience_answers_v1:', 'audience_summary_v1:', 'audience_excerpts_v1:', 'sclock:')
        result['key_families'] = {prefix: db.execute('SELECT COUNT(*) FROM kv WHERE k LIKE ?',
                                  (prefix + '%',)).fetchone()[0] for prefix in prefixes}
        attendance = value(db, 'attendees', {})
        result['attendance_records'] = {k: len(attendance.get(k, {})) for k in ('participants', 'directory')}
        result['checked_in'] = sum(bool(row.get('checked_in')) for row in attendance.get('participants', {}).values())
        result['push_subscriptions'] = len(value(db, 'participant_push_v1', {}))
        result['participant_news'] = len(value(db, 'participant_news', []))
        result['running_clocks'] = sum(bool(json.loads(r[0]).get('running')) for r in db.execute(
            "SELECT v FROM kv WHERE k='clock' OR k LIKE 'sclock:%'"))
        result['final_status'] = value(db, 'final', {}).get('status', 'idle')
        program = value(db, 'program', {})
        result['program'] = {k: program.get(k) for k in ('i', 'mode', 'scene')}
        result['program']['has_announcement'] = bool(program.get('announce'))
        result['score_display'] = value(db, 'public')
        result['result_reveal'] = value(db, 'reveal')
        if authority:
            result['authority'] = {
                'accounts': db.execute('SELECT COUNT(*) FROM person_accounts').fetchone()[0],
                'active_accounts': db.execute('SELECT COUNT(*) FROM person_accounts WHERE active=1').fetchone()[0],
                'active_credentials': db.execute('SELECT COUNT(*) FROM access_credentials WHERE active=1').fetchone()[0],
                'credential_groups': [dict(zip(('role','dataset','count'), r)) for r in db.execute(
                    'SELECT role,dataset,COUNT(*) FROM access_credentials WHERE active=1 GROUP BY role,dataset')],
                'grant_groups': [dict(zip(('role','dataset','count'), r)) for r in db.execute(
                    'SELECT g.role,g.dataset,COUNT(*) FROM account_grants g JOIN person_accounts a ON a.id=g.account_id '
                    'WHERE a.active=1 GROUP BY g.role,g.dataset')],
                'rehearsal_judges_without_live_assignment': db.execute(
                    "SELECT COUNT(*) FROM account_grants g JOIN person_accounts a ON a.id=g.account_id "
                    "WHERE a.active=1 AND g.role='judge' AND g.dataset='rehearsal' AND NOT EXISTS "
                    "(SELECT 1 FROM account_grants l WHERE l.account_id=g.account_id AND l.role='judge' AND l.dataset='live')").fetchone()[0],
                'sessions': db.execute('SELECT COUNT(*) FROM role_sessions').fetchone()[0],
            }
        return result


def inventory(root: Path, bundled_content: Path) -> dict:
    result = {'captured_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'note': 'Separate read transactions per workspace; refresh under an exclusive guard before any apply.',
              'workspaces': {}}
    for mode, folder in (('live', root), ('rehearsal', root / 'rehearsal')):
        report = inspect_database(folder / 'judging.db', authority=mode == 'live')
        source = folder / 'content.json'
        if not source.exists():
            source = bundled_content
        content = json.loads(source.read_text())
        report['content_source'] = 'workspace' if source == folder / 'content.json' else 'bundled'
        report['content_digest'] = digest(content)
        report['content_sections'] = {k: digest(v) for k, v in content.items()}
        report['agenda_presentation_digest'] = digest([
            {k: item.get(k) for k in ('time', 'min', 'title', 'scene')}
            for item in content.get('agenda', [])])
        result['workspaces'][mode] = report
    a, b = (result['workspaces'][k] for k in ('live', 'rehearsal'))
    result['different_content_sections'] = [k for k in sorted(a['content_sections'].keys() | b['content_sections'].keys())
        if a['content_sections'].get(k) != b['content_sections'].get(k)]
    result['configuration_matches'] = a['configuration'] == b['configuration']
    result['agenda_presentation_matches'] = a['agenda_presentation_digest'] == b['agenda_presentation_digest']
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--bundled-content', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inventory(args.data, args.bundled_content), indent=2))
