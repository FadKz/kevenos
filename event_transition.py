"""Reviewed, recoverable Rehearsal → Live cutover; never called at startup implicitly.

The Live SQLite connection remains authentication authority throughout. A durable
journal bridges its transaction to content.json; recovery runs before activation.
Backups contain private data and stay in owner-only directories on the server.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import uuid

import audience_questions
import database
import event_access
import event_config
import event_quiz
import event_roles

JOURNAL = '.live-transition-journal.json'
RECEIPT = 'live_transition_receipt'
TARGET = {'participant_count': 40, 'team_size': 2, 'judge_count': 8}
TABLES = {'kv', 'roster', 'roster_members', 'team_assignments', 'sheets', 'audit',
          'judge_invites', 'role_sessions', 'access_credentials', 'visitor_activity',
          'access_profiles', 'person_accounts', 'account_grants', 'participant_teams',
          'registration_links', 'notifications', 'notification_checks', 'agent_receipts',
          'agent_confirmations', 'agent_audit', 'assistant_threads', 'assistant_jobs',
          'assistant_proposals', 'sqlite_sequence'}
# Reads/audits/prepare envelopes must not invalidate the plan they just requested.
TRANSIENT = {'audit', 'visitor_activity', 'role_sessions', 'access_profiles',
             'agent_receipts', 'agent_confirmations', 'agent_audit', 'notification_checks',
             'sqlite_sequence'}


class Conflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _get(db, key, default=None):
    row = db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put(db, key, value):
    db.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, _json(value)))


def receipt(db):
    return _get(db, RECEIPT)


def _fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with open(temp, 'xb') as stream:
            os.chmod(temp, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _content(s, folder, db=None):
    if db is not None and database.is_server(db):
        row = db.execute("SELECT v FROM kv WHERE k='content_current'").fetchone()
        return json.loads(row[0]) if row else s.bundled_content()
    file = folder / 'content.json'
    return json.loads(file.read_text()) if file.exists() else s.bundled_content()


def _snapshot(db):
    tables = sorted(database.list_tables(db))
    result = {}
    for table in tables:
        if table in TRANSIENT:
            continue
        # Table names are trusted schema identifiers, quoted defensively.
        rows = [{k:({'blob_sha256':_hash(v)} if isinstance(v,bytes) else v) for k,v in dict(r).items()} for r in db.execute('SELECT * FROM "'+table.replace('"','""')+'"')]
        if table == 'access_credentials':
            for row in rows:
                row.pop('last_used', None)
        result[table] = sorted(rows, key=_json)
    return result


def _staff_credential_changes(db, accounts):
    """Remove old participant slot reservations without changing encrypted-password AAD."""
    changes=[];blockers=[]
    for row in db.execute("SELECT * FROM access_credentials WHERE role='participant' AND active=1"):
        if accounts.get(row['account_id'],{}).get('kind')!='staff':continue
        grants=[dict(g) for g in db.execute('SELECT * FROM account_grants WHERE account_id=?',(row['account_id'],)) if g['role'] in event_roles.STAFF]
        grants.sort(key=lambda g:(g['role']=='judge',g['dataset']!=row['dataset'],event_roles.ORDER.index(g['role'])))
        if not grants:
            blockers.append('A retained Staff password has no Staff role. Assign its role before preparing Live.');continue
        grant=grants[0];station=grant['station'] if grant['role']=='judge' else 0
        if grant['role']=='judge' and db.execute("SELECT 1 FROM access_credentials WHERE role='judge' AND active=1 AND dataset=? AND station=? AND id<>?",(row['dataset'],station,row['id'])).fetchone():
            blockers.append('A retained mixed-role account has duplicate judge passwords. Keep one before preparing Live.');continue
        if grant['role']=='judge' and any(role=='judge' and dataset==row['dataset'] and st==station for _,role,st,dataset in changes):
            blockers.append('Retained mixed-role passwords conflict at a judging station. Reconcile them before preparing Live.');continue
        changes.append((row['id'],grant['role'],station,row['dataset']))
    return changes,blockers


def _plan(s, guest_policy):
    c = s.WORKSPACE
    if guest_policy not in ('revoke', 'retain'):
        raise Conflict('Choose whether shared Guest access is revoked or retained.')
    if c.mode != 'rehearsal' or c.rehearsal is None:
        raise Conflict('Select Rehearsal before preparing Live.')
    blockers = []
    for name, db in [('Live', c.live), ('Rehearsal', c.rehearsal)]:
        tables = database.list_tables(db)
        expected = TABLES - ({'sqlite_sequence'} if database.is_server(db) else set())
        if _get(db, 'schema_version') != 14 or tables != expected:
            blockers.append(name + ' has an unreviewed database schema. Use the matching transition release.')
        if db.execute("SELECT count(*) FROM notifications WHERE status='sending'").fetchone()[0]:
            blockers.append(name + ' has a notification still sending. Wait for its receipt.')
        if db.execute("SELECT count(*) FROM assistant_jobs WHERE status NOT IN ('completed','failed','unknown','cancelled','expired')").fetchone()[0]:
            blockers.append(name + ' has active Assistant work. Complete or cancel it first.')
    source = _content(s, c.root / 'rehearsal', c.rehearsal)
    blockers.extend(s.validate_content(source))
    clock_values = [json.loads(r[0]) for r in c.rehearsal.execute("SELECT v FROM kv WHERE k='clock' OR k LIKE 'sclock:%'")]
    clock_values.append((_get(c.rehearsal, 'final', {}) or {}).get('clock') or {})
    if any(v.get('running') for v in clock_values):
        blockers.append('Pause all Rehearsal clocks before preparing Live.')
    accounts = {}
    credentials = [dict(r) for r in c.live.execute('SELECT * FROM access_credentials')]
    grants = [dict(r) for r in c.live.execute('SELECT * FROM account_grants')]
    for account in c.live.execute('SELECT * FROM person_accounts'):
        account = dict(account)
        roles = {g['role'] for g in grants if g['account_id'] == account['id']}
        roles |= {v['role'] for v in credentials if v['account_id'] == account['id']}
        kind = 'staff' if roles & (event_roles.STAFF | {'desk'}) else 'participant' if 'participant' in roles else 'guest'
        accounts[account['id']] = account | {'kind': kind}
    _, credential_blockers = _staff_credential_changes(c.live,accounts)
    blockers.extend(credential_blockers)
    active_ids = {v['account_id'] for v in credentials if v['active'] and accounts.get(v['account_id'], {}).get('active')}
    judges = [g for g in grants if g['role']=='judge' and g['dataset']=='rehearsal' and g['account_id'] in active_ids]
    if sorted(g['station'] for g in judges) != list(range(1, 9)):
        blockers.append('Assign exactly one active Staff judge to each Rehearsal station 1–8.')
    for g in judges:
        prior = next((p for p in grants if p['account_id']==g['account_id'] and p['role']=='judge' and p['dataset']=='live'), None)
        if prior and prior['station'] != g['station']:
            blockers.append('A retained judge has a different Live station. Reconcile judge assignments first.')
    source_ids = {g['account_id'] for g in judges}
    if any(g['role']=='judge' and g['dataset']=='live' and g['account_id'] in active_ids and g['account_id'] not in source_ids for g in grants):
        blockers.append('Live has an additional active judge assignment. Reconcile judge assignments first.')
    retained = [v['id'] for v in accounts.values() if v['kind']=='staff' or v['kind']=='guest' and guest_policy=='retain']
    retired = [v['id'] for v in accounts.values() if v['id'] not in retained]
    # Include raw content file bytes (and config files) as well as semantic data;
    # an inactive Live edit must invalidate even a seemingly unrelated plan.
    files = {}
    for folder in (() if database.is_server(c.live) else (c.root, c.root/'rehearsal')):
        for name in ('content.json', 'config.json'):
            path = folder/name
            files[str(path.relative_to(c.root))] = _hash(path.read_bytes()) if path.exists() else None
    digest = _hash(_json({'live':_snapshot(c.live), 'rehearsal':_snapshot(c.rehearsal),
                         'files':files, 'content':source, 'target':TARGET, 'guest_policy':guest_policy}).encode())
    public = {'ready':not blockers, 'blockers':blockers, 'plan_digest':digest,
              'configuration':event_config._validate(TARGET), 'guest_policy':guest_policy,
              'retained_staff_accounts':sum(v['kind']=='staff' for v in accounts.values()),
              'disabled_staff_accounts':sum(v['kind']=='staff' and not v['active'] for v in accounts.values()),
              'retired_participant_accounts':sum(v['kind']=='participant' for v in accounts.values()),
              'removed_participant_grants':sum(g['role']=='participant' for g in grants),
              'guest_accounts':sum(v['kind']=='guest' for v in accounts.values()),
              'live_judge_assignments':len(judges), 'confirmation':'PREPARE LIVE',
              'effects':['Empty Live roster, arrivals, scores, worksheets and responses.',
                         'Keep Staff/judge passwords and roles; remove all previous Participant access.',
                         'Copy Rehearsal content and question definitions; audience packs become unsent drafts.',
                         'Sign everyone out and invalidate old QR links and prepared agent actions.',
                         'Keep Rehearsal event history, audit and delivery receipts. Send no notifications.',
                         'Retain shared Guest access.' if guest_policy=='retain' else 'Revoke shared Guest access; configure new Guest access separately.']}
    return public, source, retired, judges


def preview(s, guest_policy='revoke'):
    with s.LOCK:
        return _plan(s, guest_policy)[0]


def _backup(c):
    folder = c.root/'.live-transition-backups'/uuid.uuid4().hex
    folder.mkdir(parents=True, mode=0o700)
    os.chmod(folder.parent, 0o700)
    manifest = {}
    for name, db in [('live', c.live), ('rehearsal', c.rehearsal)]:
        target = folder/(name+('.json' if database.is_server(db) else '.db'))
        # Create private before the database writer opens it, avoiding an umask exposure window.
        target.touch(mode=0o600)
        if database.is_server(db):
            database.export_snapshot(db, target)
        else:
            backup = sqlite3.connect(target)
            try:
                db.backup(backup)
                if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Conflict('Backup verification failed. No Live changes were made.')
            finally:
                backup.close()
        with target.open('rb') as stream:
            os.fsync(stream.fileno())
        manifest[target.name] = _hash(target.read_bytes())
    for prefix, root in [('live', c.root), ('rehearsal', c.root/'rehearsal')]:
        for name in ('content.json', 'config.json'):
            file = root/name
            if file.exists():
                dest = folder/(prefix+'-'+name)
                _write(dest, file.read_bytes())
                manifest[dest.name] = _hash(dest.read_bytes())
    _write(folder/'manifest.json', _json(manifest).encode())
    _fsync_dir(folder.parent)
    return folder


def recover(c):
    """Finish/roll back content publication using the SQLite commit marker."""
    if database.is_server(c.live):
        return
    journal = c.root/JOURNAL
    if not journal.exists():
        return
    try:
        data = json.loads(journal.read_text())
        folder = (c.root/data['backup']).resolve()
        if folder.parent != (c.root/'.live-transition-backups').resolve() or data['version'] != 1:
            raise ValueError('Unrecognized transition journal')
        committed = (receipt(c.live) or {}).get('transition_id') == data['transition_id']
        filename = 'next-content.json' if committed else 'live-content.json'
        digest = data['next_digest'] if committed else data['previous_digest']
        target = c.root/'content.json'
        if digest is None:
            target.unlink(missing_ok=True)
            _fsync_dir(c.root)
        else:
            content = (folder/filename).read_bytes()
            if _hash(content) != digest:
                raise ValueError('Transition recovery content failed verification')
            _write(target, content)
        journal.unlink()
        _fsync_dir(c.root)
    except Exception as error:
        c.guard.blocked = True
        raise RuntimeError('Live transition recovery is incomplete. Keep the app offline and restore the verified backup.') from error


def _activate_committed(c, control):
    try:
        c.mode, c._selection_context = 'live', control['context']
        c._activate('live')
        c._refresh_context()
    except Exception:
        c.guard.blocked = True
        raise


def _after_publish():
    """Fault-injection seam for durability tests."""


def _after_commit():
    """Fault-injection seam for durability tests."""


def apply(s, plan_digest, confirm, operation_id, actor, guest_policy='revoke'):
    c = s.WORKSPACE
    if confirm != 'PREPARE LIVE' or not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128:
        raise Conflict('Review the preview, then type PREPARE LIVE to activate the prepared event.')
    with c.write(), s.LOCK:
        previous = receipt(c.live)
        if previous and previous['operation_id']==operation_id and previous['actor']==actor:
            if previous['plan_digest'] != plan_digest or previous['guest_policy'] != guest_policy:
                raise Conflict('This operation identifier was already used for a different plan.')
            return dict(previous, replayed=True)
        plan, content, retired, judges = _plan(s, guest_policy)
        if plan_digest != plan['plan_digest']:
            raise Conflict('The event or accounts changed. Refresh the transition preview before continuing.')
        if not plan['ready']:
            raise Conflict(' '.join(plan['blockers']))
        backup = _backup(c)
        new_content = _json(content).encode()
        _write(backup/'next-content.json', new_content)
        transition_id = uuid.uuid4().hex
        journal = {'version':1, 'transition_id':transition_id, 'backup':str(backup.relative_to(c.root)),
                   'next_digest':_hash(new_content),
                   'previous_digest':_hash((backup/'live-content.json').read_bytes()) if (backup/'live-content.json').exists() else None}
        if not database.is_server(c.live):
            _write(c.root/JOURNAL, _json(journal).encode())
        control = {'mode':'live', 'context':uuid.uuid4().hex}
        result = {'applied':True, 'replayed':False, 'transition_id':transition_id,
                  'operation_id':operation_id, 'actor':actor, 'plan_digest':plan_digest,
                  'guest_policy':guest_policy, 'context':control['context'], 'mode':'live',
                  'backup_reference':str(backup.relative_to(c.root)), 'created':time.time(),
                  'retained_staff_accounts':plan['retained_staff_accounts'],
                  'retired_participant_accounts':plan['retired_participant_accounts'],
                  'configuration':plan['configuration']}
        db = c.live
        staff_accounts={row['id']:{'kind':'staff'} for row in db.execute('SELECT id FROM person_accounts') if row['id'] not in retired}
        credential_changes,credential_blockers=_staff_credential_changes(db,staff_accounts)
        if credential_blockers:raise Conflict(' '.join(credential_blockers))
        try:
            database.begin_immediate(db)
            for table in ('roster','roster_members','team_assignments','sheets','participant_teams','judge_invites','registration_links','role_sessions','agent_confirmations'):
                db.execute('DELETE FROM '+table)
            db.execute('UPDATE access_profiles SET revoked=1')
            db.execute('UPDATE assistant_threads SET closed=1')
            db.execute("UPDATE assistant_proposals SET status='expired' WHERE status NOT IN ('applied','completed','failed','expired','cancelled')")
            db.execute("DELETE FROM account_grants WHERE role='participant'")
            for credential_id,staff_role,station,_dataset in credential_changes:
                # Password box binds id/revision/dataset: leave all three and hashes unchanged.
                db.execute('UPDATE access_credentials SET role=?,pair=0,member=0,station=? WHERE id=?',(staff_role,station,credential_id))
            for account_id in retired:
                db.execute('UPDATE person_accounts SET active=0,revision=revision+1 WHERE id=?', (account_id,))
                db.execute('UPDATE access_credentials SET active=0,password_box=NULL,revision=revision+1 WHERE account_id=?', (account_id,))
            for g in judges:
                db.execute("INSERT INTO account_grants(account_id,role,dataset,station,pair,member,subject) VALUES(?,'judge','live',?,0,0,'') ON CONFLICT(account_id,role,dataset) DO UPDATE SET station=excluded.station,pair=0,member=0,subject=''", (g['account_id'],g['station']))
            db.execute("DELETE FROM kv WHERE k NOT IN ('schema_version')")
            quiz = event_quiz.empty_quiz()
            old_quiz = _get(c.rehearsal, event_quiz.KEY, {})
            quiz.update(enabled=True, edits=deepcopy(old_quiz.get('edits', {})), prize=old_quiz.get('prize', ''))
            packs = []
            for pack in _get(c.rehearsal, audience_questions.KEY, {'packs':[]})['packs']:
                raw = {k:pack[k] for k in ('title','description') if k in pack}
                raw['questions'] = [{k:v for k,v in q.items() if k!='id'} for q in pack['questions']]
                packs.append(audience_questions.build_pack(raw, None))
            values = {'workspace_control':control, 'event_configuration':TARGET,
                      'mode':'desk', 'clock':s._fresh_clock(), 'public':'off', 'reveal':'hidden',
                      'program':{'i':0,'since':time.time(),'mode':'manual','scene':'auto','announce':''},
                      event_quiz.KEY:quiz, audience_questions.KEY:{'rev':0,'packs':packs}, RECEIPT:result}
            if database.is_server(db):
                values['content_current'] = content
                values['content_history:%d' % int(content.get('content_version', 1))] = content
            for key, value in values.items():
                _put(db, key, value)
            scenario_source = SimpleNamespace(CONTENT=content)
            db.executemany('INSERT INTO team_assignments(pair,station,round_no,scenario) VALUES(?,?,?,?)',
                           [(pair,(pair-1)%8+1,(pair-1)//8+1,event_config._default_scenario(scenario_source,pair,8)) for pair in range(1,21)])
            db.execute('INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)',
                       (time.time(),actor,'live-transition',_json({'transition_id':transition_id,'plan_digest':plan_digest,'backup_reference':result['backup_reference']})))
            if not database.is_server(db):
                _write(c.root/'content.json', new_content)
            _after_publish()
            db.commit()
            _after_commit()
        except BaseException:
            db.rollback()
            # BaseException simulates abrupt termination in tests. Real process
            # death leaves the journal for Controller.__init__ on restart.
            recover(c)
            if (receipt(db) or {}).get('transition_id') == transition_id:
                _activate_committed(c, control)
            raise
        recover(c)
        _activate_committed(c, control)
        return result


def handle(s, h, body=None):
    ident = event_access.session(s, h.headers)
    if not ident or ident['role']!='admin' or ident.get('impersonation'):
        h.send(403 if ident else 401, {'error':'Sign in directly as Admin to prepare Live.'})
        return True
    try:
        body = body or {}
        policy = body.get('guest_policy', 'revoke')
        if body.get('action', 'preview') == 'preview':
            result = preview(s, policy)
        elif body['action'] == 'apply':
            result = apply(s,body.get('plan_digest'),body.get('confirm'),body.get('operation_id'),
                           ident.get('credential_id') or 'owner-admin',policy)
        else:
            raise Conflict('Choose Preview or Prepare Live.')
        h.send(200, result)
    except (Conflict, ValueError) as error:
        h.send(409, {'error':str(error)})
    return True
