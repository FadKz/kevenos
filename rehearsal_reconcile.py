"""Preview and apply a rehearsal size change without replacing the workspace."""
import hashlib
import json
import re
import uuid

import event_access
import event_config


class ResizeConflict(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def seed_member(pair, member):
    return 'PracticePerson%02d%s' % (pair, chr(64 + member))


def seed_team(pair):
    return 'PracticeTeam%02d' % pair


def seed_sheet(station):
    return {
        'kind': 'round', 'pair': station, 'station': station, 'round': 1,
        'judge': 'PracticeJudge%02d' % station, 'ticks': [i < (5 + station % 5) for i in range(10)],
        'ai': station % 2, 'spoke': 1, 'notes': 'Sample rehearsal notes',
        'feedback': 'Sample rehearsal feedback', 'asked': [], 'raised': [],
        'voided': 0, 'edited_by': None, 'final_run': '',
    }


def _rows(db, query):
    return [dict(row) for row in db.execute(query)]


def _workspace(s):
    workspace = s.WORKSPACE
    if not workspace or workspace.mode != 'rehearsal':
        raise ResizeConflict('rehearsal_required', 'Select the rehearsal workspace to reconcile its event size.')
    return workspace


def _sample_sheets(sheets, fixture, seeded, current):
    if not seeded:
        return set()
    verified = set()
    for raw in sheets:
        if fixture:
            if fixture['sheets'].get(str(raw['id'])) == _digest(raw):
                verified.add(raw['id'])
            continue
        # Legacy seeds were the first row IDs, one unchanged ballot per
        # station. User submissions are appended and replacements void these.
        station = raw['station']
        if (type(station) is not int or raw['id'] != station
                or not 1 <= station <= min(32, current['team_count'], current['judge_count'])
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', raw.get('app_version') or '')
                or raw.get('content_version') != '1' or not raw.get('created')):
            continue
        try:
            candidate = {key: json.loads(raw[key]) if key in ('ticks', 'asked', 'raised') else raw[key]
                         for key in seed_sheet(station)}
        except (TypeError, ValueError):
            continue
        if candidate == seed_sheet(station) and all(type(tick) is bool for tick in candidate['ticks']):
            verified.add(raw['id'])
    return verified


def _sample_member(pair, member, values, fixture, seeded):
    if not seeded or values != {'name': seed_member(pair, member), 'coach': 0}:
        return False
    return not fixture or member <= event_config.member_capacity(fixture['configuration'], pair)


def _untouched_legacy_final(final, sheets, audit):
    # Migration inferred "ready" from the old active flag. That flag can
    # exist before any judging occurred, but it also covers a paused pitch.
    if (final.get('status') != 'ready' or final.get('legacy') is not True
            or not re.fullmatch(r'legacy-[0-9a-f]{32}', final.get('run_id') or '')
            or final.get('active') is not True or final.get('current', 0) != 0
            or not final.get('pairs') or final.get('visited') != final['pairs'][:1]
            or any(final.get(field) for field in ('legacy_ballot_ids', 'waivers', 'published',
                                                  'published_at', 'publication_history', 'published_rows',
                                                  'decision', 'started_at'))
            or any(row['kind'] == 'final' for row in sheets)
            or audit):
        return False
    clock = final.get('clock')
    return clock is None or (
        isinstance(clock, dict) and clock.get('running') is False
        and clock.get('elapsed') == 0 and clock.get('started') is None
        and clock.get('ended', False) is False
    )


def _input(s):
    return {
        'roster': _rows(s.DB, 'SELECT * FROM roster ORDER BY pair'),
        'members': _rows(s.DB, 'SELECT * FROM roster_members ORDER BY pair,member'),
        'assignments': _rows(s.DB, 'SELECT * FROM team_assignments ORDER BY pair'),
        'sheets': _rows(s.DB, 'SELECT * FROM sheets ORDER BY id'),
        'credentials': _rows(event_access.auth_db(s), """
            SELECT c.id,g.role,g.pair,g.member,g.station,CASE WHEN g.role='participant' THEN g.subject ELSE c.label END AS label,c.revision FROM access_credentials c
            JOIN person_accounts a ON a.id=c.account_id AND a.active=1 JOIN account_grants g ON g.account_id=a.id
            WHERE g.dataset='rehearsal' AND c.active=1 AND g.role IN ('participant','judge') ORDER BY c.id,g.role
        """),
        'final_activity': _rows(s.DB, """
            SELECT id FROM audit
             WHERE action IN ('final-start','final-next','final-goto','final-stop','final-review',
                              'final-override','final-publish','final-reopen')
                OR (action='final-clock' AND detail NOT IN ('reset','pause'))
             ORDER BY id LIMIT 1
        """),
        'invites': _rows(s.DB, 'SELECT station FROM judge_invites WHERE used=0 ORDER BY station'),
        'state': _rows(s.DB, """
            SELECT k,v FROM kv WHERE k IN (
                'event_configuration','final','attendees','workspace_seeded','rehearsal_fixtures','clock','mode'
            ) OR k LIKE 'sclock:%' ORDER BY k
        """),
        'content': s.CONTENT,
    }


def _plan(s, target):
    workspace = _workspace(s)
    current = event_config.config(s, read_only=True)
    original = _input(s)
    state = {row['k']: json.loads(row['v']) for row in original['state']}
    fixture, seeded = state.get('rehearsal_fixtures'), state.get('workspace_seeded')
    changed = target != current
    blockers = []

    def block(code, message):
        if code not in {item['code'] for item in blockers}:
            blockers.append({'code': code, 'message': message})

    if changed and event_config._running(s):
        block('clocks_running', 'Pause the judging clocks before resizing rehearsal.')
    final = state.get('final', {})
    untouched_legacy_final = _untouched_legacy_final(final, original['sheets'], original['final_activity'])
    if changed and event_config._final_started(s) and not untouched_legacy_final:
        block('final_started', 'The Final has started. Its run and results must be preserved.')

    participants = {(row['pair'], row['member']): row for row in original['credentials'] if row['role'] == 'participant'}
    judges = {row['station'] for row in original['credentials'] if row['role'] == 'judge'}
    judges.update(row['station'] for row in original['invites'])
    if changed and any(station > target['judge_count'] for station in judges):
        block('judge_access_outside_capacity', 'An issued rehearsal judge account or invitation is outside the new station capacity.')

    attendees = state.get('attendees') or {'participants': {}, 'directory': {}}
    attendee_slots = {tuple(map(int, key.split(':'))): item for key, item in attendees['participants'].items()}
    roster = {row['pair']: row for row in original['roster']}
    members = {}
    for row in original['members']:
        members.setdefault(row['pair'], {})[row['member']] = {'name': row['name'], 'coach': row['coach']}
    for pair, row in roster.items():
        # The first two columns remain authoritative over legacy copies.
        for slot in (1, 2):
            value = {'name': row[f'p{slot}'] or '', 'coach': row[f'coach{slot}'] or 0}
            if any(value.values()) or slot in members.get(pair, {}):
                members.setdefault(pair, {})[slot] = value

    fixture_sheets = _sample_sheets(original['sheets'], fixture, seeded, current)
    protected_sheets = [row for row in original['sheets'] if row['id'] not in fixture_sheets]
    scored = {row['pair'] for row in protected_sheets if row['kind'] in ('round', 'final')}
    assignments = {row['pair']: row for row in original['assignments']}
    team_ids = sorted(set(roster) | set(members) | {pair for pair, _ in participants} |
                      {pair for pair, _ in attendee_slots} | scored)
    protected = {}
    pinned = set(scored)
    pinned.update(pair for pair, _ in participants)
    for pair in team_ids:
        company = (roster.get(pair, {}).get('partner') or '')
        company_is_sample = bool(seeded and company == seed_team(pair) and (
            not fixture or pair <= fixture['configuration']['team_count']))
        team_members = members.get(pair, {})
        protected_slots = {member for member, value in team_members.items()
                           if any(value.values()) and not _sample_member(pair, member, value, fixture, seeded)}
        protected_slots.update(member for p, member in participants if p == pair)
        if pair in scored:
            protected_slots.update(member for member, value in team_members.items() if any(value.values()))
        protected_slots.update(member for (p, member), item in attendee_slots.items()
                               if p == pair and item.get('name') == team_members.get(member, {}).get('name'))
        if not protected_slots and pair not in pinned and (not company or company_is_sample):
            continue
        protected[pair] = {'partner': company, 'members': team_members, 'protected': protected_slots}

    max_slots = target['team_count']
    locations = {}
    free = set(range(1, max_slots + 1))
    for pair in sorted(pinned):
        if pair not in free or any(member > event_config.member_capacity(target, pair)
                                   for member in protected[pair]['protected']):
            if pair in scored:
                block('protected_scores', f'Team {pair} has a protected judging sheet outside the proposed team or slot capacity.')
            else:
                block('participant_access_outside_capacity', f'Team {pair} has issued rehearsal participant access outside the proposed capacity.')
            continue
        locations[pair] = pair
        free.remove(pair)

    # There is at most one partial team. Seat larger teams first so a smaller
    # team can use that slot. Prefer the current number whenever it fits.
    unpinned = sorted((pair for pair in protected if pair not in pinned),
                      key=lambda pair: (-len(protected[pair]['protected']), pair))
    for pair in unpinned:
        eligible = sorted(slot for slot in free
                          if event_config.member_capacity(target, slot) >= len(protected[pair]['protected']))
        if not eligible:
            block('protected_roster', f'Team {pair} cannot fit while preserving its registered members. Increase capacity or adjust the rehearsal roster.')
            continue
        selected = pair if pair in eligible else eligible[0]
        locations[pair] = selected
        free.remove(selected)

    result_rows = {}
    slot_map = {}
    for pair, destination in locations.items():
        team = protected[pair]
        limit = event_config.member_capacity(target, destination)
        used = {member for member in team['protected'] if member <= limit}
        mapping = {member: member for member in used}
        vacancies = iter(sorted(set(range(1, limit + 1)) - used))
        for member in sorted(team['protected'] - used):
            mapping[member] = next(vacancies)
        reserved = set(mapping.values())
        for member in sorted(team['members']):
            if member not in mapping and member <= limit and member not in reserved:
                mapping[member] = member
        for member, slot in mapping.items():
            slot_map[pair, member] = (destination, slot)
        result_rows[destination] = {'partner': team['partner'], 'members': {
            slot: team['members'].get(member, {'name': '', 'coach': 0}) for member, slot in mapping.items()
        }}

    # Keep fitting verified sample teams in their existing slots. Expansion
    # leaves new capacity empty so a rehearsal can be filled with real people.
    for pair in sorted(free & set(roster)):
        if pair in protected:
            continue
        limit = event_config.member_capacity(target, pair)
        result_rows[pair] = {'partner': roster[pair]['partner'], 'members': {
            member: value for member, value in members.get(pair, {}).items() if member <= limit
        }}

    result_assignments = {}
    locations_by_destination = {destination: pair for pair, destination in locations.items()}
    scenario_ids = {scenario['id'] for scenario in s.CONTENT['scenarios']}
    open_slots = [(round_no, station) for round_no in range(1, target['rounds'] + 1)
                  for station in range(1, target['judge_count'] + 1)]
    available = set(open_slots)
    for row in protected_sheets:
        if row['kind'] == 'calib':
            if row['station'] > target['judge_count']:
                block('protected_calibration', 'The new station capacity would exclude a recorded calibration sheet.')
            continue
        pair = row['pair']
        prior = assignments.get(pair)
        if row['kind'] != 'round':
            block('protected_final_sheets', 'Existing Final judging sheets must be preserved before resizing.')
            continue
        slot = (row['round'], row['station'])
        if (pair not in locations or not prior or slot != (prior['round_no'], prior['station'])
                or (slot not in available and pair not in result_assignments)):
            block('protected_scores', f'Team {pair} has a protected judging sheet incompatible with the proposed schedule.')
            continue
        result_assignments[pair] = dict(prior)
        available.discard(slot)

    def source_assignment(pair):
        source = locations_by_destination.get(pair, pair)
        # A moved protected team's original case belongs to that team, even
        # when its old number now identifies a vacant or different team.
        if source == pair and locations.get(pair, pair) != pair:
            return None
        return assignments.get(source)

    for pair in range(1, max_slots + 1):
        prior = source_assignment(pair)
        if pair in result_assignments or not prior:
            continue
        slot = (prior['round_no'], prior['station'])
        if slot in available:
            result_assignments[pair] = dict(prior, pair=pair)
            available.remove(slot)

    remaining = iter(slot for slot in open_slots if slot in available)
    for pair in range(1, max_slots + 1):
        if pair in result_assignments:
            continue
        round_no, station = next(remaining)
        prior = source_assignment(pair)
        position = (round_no - 1) * target['judge_count'] + station
        scenario = prior['scenario'] if prior else event_config._default_scenario(s, position, target['judge_count'])
        result_assignments[pair] = {'pair': pair, 'station': station, 'round_no': round_no, 'scenario': scenario}

    for pair, assignment in result_assignments.items():
        if assignment['scenario'] not in scenario_ids:
            block('missing_scenario', f'Team {pair} has an assignment with a missing scenario.')

    for pair, member in participants:
        credential = participants[pair, member]
        if members.get(pair, {}).get(member, {}).get('name') != credential['label']:
            block('credential_roster_mismatch', f'An issued rehearsal credential for team {pair}, member {member} does not match the current roster.')

    retained_attendees = {}
    for old, item in attendee_slots.items():
        if old in slot_map and item.get('name') == members.get(old[0], {}).get(old[1], {}).get('name'):
            pair, member = slot_map[old]
            retained_attendees[f'{pair}:{member}'] = item
    after_attendees = {'participants': retained_attendees, 'directory': attendees['directory']}
    if changed and len(retained_attendees) != len(attendee_slots):
        block('protected_checkins', 'A participant check-in does not match a retained member slot. Resolve the rehearsal roster before resizing.')

    moves = [{'from_pair': pair, 'to_pair': dest} for pair, dest in sorted(locations.items()) if pair != dest]
    retained_members = sum(bool(value['name'].strip()) for team in result_rows.values() for value in team['members'].values())
    original_members = sum(bool(value['name'].strip()) for team in members.values() for value in team.values())
    clear_final = final.get('status') == 'prepared' or untouched_legacy_final
    warnings = event_config.schedule_warnings(s, target)
    if changed:
        warnings.append('Preliminary clocks will return to the first round. Published programme times stay the same.')
        if untouched_legacy_final:
            warnings.append('The migrated Final setup has no recorded judging and will be cleared. Select finalists and judges again.')
        elif clear_final:
            warnings.append('The prepared Final will be cleared. Select finalists and judges again before starting it.')
    data = {'rows': result_rows, 'assignments': result_assignments, 'attendees': after_attendees,
            'remove_sheets': sorted(fixture_sheets), 'clear_final': clear_final}
    report = {
        'configuration': target, 'current': current, 'changed': changed, 'can_apply': not changed or not blockers,
        'blockers': blockers if changed else [], 'warnings': warnings, 'moves': moves if changed else [],
        'summary': {'protected_teams': len(protected), 'retained_teams': len(result_rows),
                    'retained_members': retained_members, 'removed_sample_members': max(0, original_members - retained_members) if not blockers and changed else 0,
                    'removed_sample_sheets': len(fixture_sheets) if changed else 0,
                    'retained_judging_sheets': len(protected_sheets), 'retained_credentials': len(original['credentials']),
                    'retained_checkins': len(retained_attendees), 'retained_directory_entries': len(attendees['directory'])},
        'plan_digest': _digest([1, workspace.context, target, original, data, blockers]),
    }
    if not changed:
        report['summary'].update(retained_teams=len(roster), retained_members=original_members,
                                 retained_judging_sheets=len(original['sheets']), retained_checkins=len(attendee_slots))
    return report, data


def preview(s, values):
    target = event_config._validate(values)
    workspace = _workspace(s)
    with workspace.read(), s.LOCK:
        return _plan(s, target)[0]


def _put(s, key, value):
    s.DB.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                 (key, json.dumps(value)))


def _write_plan(s, target, data):
    import participant_workspace
    participant_workspace.reconcile(s, [
        {'pair': pair, 'partner': row['partner'], 'members': [
            {'id': member, 'name': value['name']} for member, value in row['members'].items()
        ]} for pair, row in data['rows'].items()
    ])
    s.DB.execute('DELETE FROM roster_members')
    s.DB.execute('DELETE FROM roster')
    for pair, row in data['rows'].items():
        members = row['members']
        first = members.get(1, {'name': '', 'coach': 0})
        second = members.get(2, {'name': '', 'coach': 0})
        s.DB.execute('INSERT INTO roster(pair,partner,p1,p2,coach1,coach2) VALUES(?,?,?,?,?,?)',
                     (pair, row['partner'], first['name'], second['name'], first['coach'], second['coach']))
        s.DB.executemany('INSERT INTO roster_members(pair,member,name,coach) VALUES(?,?,?,?)',
                         [(pair, slot, value['name'], value['coach']) for slot, value in members.items()])
    s.DB.execute('DELETE FROM team_assignments')
    s.DB.executemany('INSERT INTO team_assignments(pair,station,round_no,scenario) VALUES(:pair,:station,:round_no,:scenario)',
                     data['assignments'].values())
    s.DB.executemany('DELETE FROM sheets WHERE id=?', [(sheet_id,) for sheet_id in data['remove_sheets']])
    s.DB.execute("DELETE FROM kv WHERE k LIKE 'sclock:%'")
    _put(s, 'clock', s._fresh_clock())
    _put(s, 'attendees', data['attendees'])
    if data['clear_final']:
        s.DB.execute("DELETE FROM kv WHERE k='final'")
        _put(s, 'reveal', 'hidden')
    event_config._put(s.DB, target)


def apply(s, values, plan_digest):
    target = event_config._validate(values)
    workspace = _workspace(s)
    if not isinstance(plan_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', plan_digest):
        raise ResizeConflict('plan_required', 'Preview the new rehearsal size before applying it.')
    with workspace.write(), s.LOCK:
        _workspace(s)
        receipt = s.kv_get('rehearsal_resize_receipt')
        if (receipt and receipt['plan_digest'] == plan_digest and receipt['configuration'] == target
                and event_config.config(s, read_only=True) == target and receipt['context'] == workspace.context):
            return {**receipt, 'replayed': True}
        report, data = _plan(s, target)
        if plan_digest != report['plan_digest']:
            raise ResizeConflict('stale_resize_plan', 'Rehearsal changed. Preview the resize again before applying it.')
        if not report['can_apply']:
            raise ResizeConflict('resize_blocked', report['blockers'][0]['message'])
        if not report['changed']:
            return {**report, 'applied': False, 'replayed': False, 'context': workspace.context}
        epoch = uuid.uuid4().hex
        result = {**report, 'applied': True, 'replayed': False,
                  'context': workspace.rehearsal_context(epoch)}
        with s.DB:
            _write_plan(s, target, data)
            _put(s, 'rehearsal_resize_epoch', epoch)
            _put(s, 'rehearsal_resize_receipt', result)
        workspace.context = result['context']
        event_config._apply_aliases(s, target)
        s.LIVE.clear()
        return result
