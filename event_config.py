"""Workspace event sizing, stable team assignments and variable membership.

Legacy roster columns remain the first two member slots so existing credentials
and backups keep their identities. Additional members have stable numbered slots.
"""
import json
import math


KEY = 'event_configuration'
LIMITS = {'participant_count': (1, 1000), 'team_size': (1, 10), 'judge_count': (1, 32)}


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS roster_members (
        pair INTEGER NOT NULL, member INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
        coach INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(pair,member))''')
    db.execute('''CREATE TABLE IF NOT EXISTS team_assignments (
        pair INTEGER PRIMARY KEY, station INTEGER NOT NULL, round_no INTEGER NOT NULL,
        scenario TEXT NOT NULL)''')


def _derive(values):
    result = {key: values[key] for key in LIMITS}
    result['team_count'] = math.ceil(result['participant_count'] / result['team_size'])
    result['rounds'] = math.ceil(result['team_count'] / result['judge_count'])
    result['round_count'] = result['rounds']
    result['pitch_block_minutes'] = result['rounds'] * 15
    result['incomplete_team_size'] = result['participant_count'] % result['team_size']
    result['final_panel'] = list(range(1, result['judge_count'] + 1))
    result['badge_cap'] = 4
    return result


def _validate(values):
    for key, (low, high) in LIMITS.items():
        if type(values.get(key)) is not int or not low <= values[key] <= high:
            raise ValueError('%s must be a whole number from %d to %d.' % (key.replace('_', ' ').capitalize(), low, high))
    return _derive(values)


def _defaults(s):
    return _derive({'participant_count': s.CFG.get('participant_count', s.CFG.get('pairs', 30) * s.CFG.get('team_size', 2)),
                    'team_size': s.CFG.get('team_size', 2), 'judge_count': s.CFG.get('stations', 8)})


def _read(db):
    row = db.execute('SELECT v FROM kv WHERE k=?', (KEY,)).fetchone()
    return _validate(json.loads(row['v'])) if row else None


def _put(db, values):
    stored = {key: values[key] for key in LIMITS}
    db.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (KEY, json.dumps(stored)))


def _apply_aliases(s, c):
    s.CFG.update(participant_count=c['participant_count'], team_size=c['team_size'],
                 pairs=c['team_count'], stations=c['judge_count'], rounds=c['rounds'])


def _default_scenario(s, pair, stations):
    scenarios = s.CONTENT.get('scenarios', [])
    if not scenarios:
        raise ValueError('Load event scenarios before initialising event assignments.')
    station = (pair - 1) % stations + 1
    for scenario in scenarios:
        if station in scenario.get('stations', []):
            return scenario['id']
    # Larger panels cycle through the available cases rather than silently
    # assigning all additional teams the first case.
    return scenarios[(station - 1) % len(scenarios)]['id']


def initialize_db(s, db, values=None):
    """Initialise one database without changing the active workspace."""
    migrate(db)
    existing = _read(db)
    c = existing or _validate(values or _defaults(s))
    if existing:
        count = db.execute('SELECT count(*) FROM team_assignments WHERE pair BETWEEN 1 AND ?', (c['team_count'],)).fetchone()[0]
        if count == c['team_count']:
            return c
    with s.LOCK, db:
        if not existing:
            _put(db, c)
        for pair in range(1, c['team_count'] + 1):
            db.execute('INSERT OR IGNORE INTO team_assignments(pair,station,round_no,scenario) VALUES(?,?,?,?)',
                       (pair, (pair - 1) % c['judge_count'] + 1, (pair - 1) // c['judge_count'] + 1,
                        _default_scenario(s, pair, c['judge_count'])))
    return c


def activate(s, values=None):
    c = initialize_db(s, s.DB, values)
    _apply_aliases(s, c)
    return c


def config(s, read_only=False):
    with s.LOCK:
        c = _read(s.DB)
    if c is None:
        return _defaults(s) if read_only else activate(s)
    _apply_aliases(s, c)
    return c


def member_capacity(c, pair):
    return max(0, min(c['team_size'], c['participant_count'] - (pair - 1) * c['team_size']))


def assignment_for(s, pair, read_only=False):
    if pair is None:
        return None
    c = config(s, read_only=read_only)
    if type(pair) is not int or not 1 <= pair <= c['team_count']:
        return None
    with s.LOCK:
        row = s.DB.execute('SELECT pair,station,round_no AS round,scenario FROM team_assignments WHERE pair=?', (pair,)).fetchone()
    if row is None and read_only:
        return {'pair': pair, 'station': (pair - 1) % c['judge_count'] + 1,
                'round': (pair - 1) // c['judge_count'] + 1,
                'scenario': _default_scenario(s, pair, c['judge_count'])}
    if row is None:
        activate(s)
        with s.LOCK:
            row = s.DB.execute('SELECT pair,station,round_no AS round,scenario FROM team_assignments WHERE pair=?', (pair,)).fetchone()
    return dict(row)


def original_scenario(s, pair, read_only=False):
    assignment = assignment_for(s, pair, read_only=read_only)
    if assignment is None:
        return None
    scenario = next((item for item in s.CONTENT['scenarios'] if item['id'] == assignment['scenario']), None)
    if scenario is None:
        raise ValueError('The original scenario for this team is no longer available.')
    return scenario


def pair_for(s, round_no, station, read_only=False):
    c = config(s, read_only=read_only)
    with s.LOCK:
        row = s.DB.execute('SELECT pair FROM team_assignments WHERE round_no=? AND station=? AND pair<=?',
                           (round_no, station, c['team_count'])).fetchone()
    if row:
        return row['pair']
    if read_only and _read(s.DB) is None and 1 <= station <= c['judge_count']:
        pair = (round_no - 1) * c['judge_count'] + station
        return pair if 1 <= pair <= c['team_count'] else None
    return None


def members(s, row, capacity=None):
    """Read member slots while honouring legacy first-two-column updates."""
    row = dict(row)
    c = config(s)
    capacity = member_capacity(c, row['pair']) if capacity is None else capacity
    with s.LOCK:
        extras = {r['member']: dict(r) for r in s.DB.execute('SELECT * FROM roster_members WHERE pair=?', (row['pair'],))}
    result = []
    for member in range(1, capacity + 1):
        if member <= 2:
            name, coach = row.get('p'+str(member)) or '', row.get('coach'+str(member)) or 0
        else:
            value = extras.get(member, {})
            name, coach = value.get('name', ''), value.get('coach', 0)
        result.append({'id': member, 'name': name, 'coach': int(bool(coach))})
    return result


def roster_row(s, pair):
    if pair is None:
        return None
    assignment = assignment_for(s, pair)
    if assignment is None:
        return None
    with s.LOCK:
        row = s.DB.execute('SELECT * FROM roster WHERE pair=?', (pair,)).fetchone()
    result = dict(row) if row else {'pair': pair, 'partner': 'Team %d' % pair, 'p1': '', 'p2': '', 'coach1': 0, 'coach2': 0}
    result['members'] = members(s, result)
    result['badge'] = min(4, 2 * sum(item['coach'] for item in result['members']))
    result.update(assignment)
    return result


def list_roster(s):
    config(s)
    with s.LOCK:
        pairs = [row['pair'] for row in s.DB.execute('SELECT pair FROM roster WHERE pair<=? ORDER BY pair', (s.CFG['pairs'],))]
    return [roster_row(s, pair) for pair in pairs]


def _running(s):
    for row in s.DB.execute("SELECT k,v FROM kv WHERE k='clock' OR k LIKE 'sclock:%'"):
        if json.loads(row['v']).get('running'):
            return True
    return False


def _final_started(s):
    final = s.kv_get('final', {})
    return bool(final.get('active') or final.get('status') not in (None, '', 'idle', 'prepared'))


def _invalidate_prepared_final(s):
    if s.kv_get('final', {}).get('status') != 'prepared':
        return False
    s.DB.execute("DELETE FROM kv WHERE k='final'")
    s.DB.execute("INSERT INTO kv(k,v) VALUES('reveal',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps('hidden'),))
    return True


def blocked_reason(s):
    if _final_started(s):
        return 'Final judging has begun. Event settings are locked to preserve the panel and results.'
    if _running(s):
        return 'Pause the event clocks before changing event settings.'
    if s.DB.execute("SELECT 1 FROM sheets WHERE kind IN ('round','final') AND voided=0 LIMIT 1").fetchone():
        return 'Scoring sheets already exist. Event settings are locked to preserve assignments and scores.'
    return ''


def status(s):
    c = config(s)
    rows = list_roster(s)
    count = sum(bool(item['name'].strip()) for row in rows for item in row['members'])
    reason = blocked_reason(s)
    warnings = schedule_warnings(s, c)
    return {'configuration': c, 'summary': {'registered_teams': sum(any(m['name'].strip() for m in row['members']) for row in rows),
                                           'participants': count, 'unfilled_slots': c['participant_count'] - count},
            'editable': not bool(reason), 'reason': reason, 'warnings': warnings}


def schedule_warnings(s, c=None):
    c = c or config(s, read_only=True)
    warnings = []
    if c['incomplete_team_size']:
        warnings.append('The last team has %d of %d member slots.' % (c['incomplete_team_size'], c['team_size']))
    pitch = next((a for a in s.CONTENT.get('agenda', []) if a.get('scene') == 'pitch'), None)
    # The schedule remains published as entered; sizing never changes start times.
    if pitch and isinstance(pitch.get('min'), (int, float)) and pitch['min'] < c['pitch_block_minutes']:
        warnings.append('The pitch rounds need %d minutes; the published block allows %d. Review the programme.' % (c['pitch_block_minutes'], pitch['min']))
    return warnings


def update(s, values):
    current = config(s)
    candidate = _validate(values)
    if all(candidate[key] == current[key] for key in LIMITS):
        return status(s)
    with s.LOCK:
        reason = blocked_reason(s)
        if reason:
            raise ValueError(reason)
        rows = list_roster(s)
        for row in rows:
            limit = member_capacity(candidate, row['pair'])
            if (row['pair'] > candidate['team_count'] and row['partner'].strip()) or any(item['name'].strip() for item in row['members'] if item['id'] > limit):
                raise ValueError('The new participant capacity or team size would remove an existing member. Update the roster first.')
        with s.DB:
            cleared_final = _invalidate_prepared_final(s)
            _put(s.DB, candidate)
            # A new pre-scoring schedule starts ready at its first assignment.
            # Old paused/ended clocks can point beyond the resized event.
            s.DB.execute("DELETE FROM kv WHERE k LIKE 'sclock:%'")
            s.DB.execute("INSERT INTO kv(k,v) VALUES('clock',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                         (json.dumps(s._fresh_clock()),))
            for pair in range(1, candidate['team_count'] + 1):
                old = s.DB.execute('SELECT scenario FROM team_assignments WHERE pair=?', (pair,)).fetchone()
                scenario = old['scenario'] if old else _default_scenario(s, pair, candidate['judge_count'])
                s.DB.execute('INSERT INTO team_assignments(pair,station,round_no,scenario) VALUES(?,?,?,?) '
                             'ON CONFLICT(pair) DO UPDATE SET station=excluded.station,round_no=excluded.round_no',
                             (pair, (pair - 1) % candidate['judge_count'] + 1,
                              (pair - 1) // candidate['judge_count'] + 1, scenario))
        _apply_aliases(s, candidate)
        s.LIVE.clear()
    result = status(s)
    result['warnings'].append('Event size changed. Preliminary clocks are paused at the first round; judges should check their new assignment.')
    if cleared_final:
        result['warnings'].append('Event setup changed. Select the finalists and panel again before starting the Final.')
    return result


def _normalise_row(s, item, c):
    if not isinstance(item, dict):
        if not isinstance(item, (tuple, list)) or len(item) != 6:
            raise ValueError('Invalid team row.')
        item = dict(zip(('pair', 'partner', 'p1', 'p2', 'coach1', 'coach2'), item))
    pair = item.get('pair')
    if type(pair) is not int or not 1 <= pair <= c['team_count']:
        raise ValueError('Choose a team number within the configured capacity.')
    capacity = member_capacity(c, pair)
    values = item.get('members')
    if values is None:
        previous = roster_row(s, pair)
        values = [{'id': i, 'name': item.get('p'+str(i), previous['members'][i-1]['name']),
                   'coach': item.get('coach'+str(i), previous['members'][i-1]['coach'])} for i in range(1, capacity+1)]
    if not isinstance(values, list) or len(values) > capacity:
        raise ValueError('This team has too many members for the configured participant capacity.')
    by_id = {}
    for index, value in enumerate(values, 1):
        value = {'name': value, 'id': index} if isinstance(value, str) else value
        if not isinstance(value, dict):
            raise ValueError('Each team member needs a name.')
        member = value.get('id', index)
        if type(member) is not int or not 1 <= member <= capacity or member in by_id:
            raise ValueError('Team member IDs must be unique slots within the configured team size.')
        name = value.get('name', '')
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise ValueError('Team member names must be at most 80 characters.')
        by_id[member] = {'id': member, 'name': name.strip(), 'coach': int(bool(value.get('coach')))}
    partner = item.get('partner', '')
    if not isinstance(partner, str) or len(partner.strip()) > 80:
        raise ValueError('Company names must be at most 80 characters.')
    return {'pair': pair, 'partner': partner.strip(), 'members': [by_id.get(i, {'id': i, 'name': '', 'coach': 0}) for i in range(1, capacity+1)]}


def save_roster(s, raw_rows, *, allow_empty=False):
    import event_access
    import participant_workspace
    from event_commands import reconcile_attendees
    c = config(s)
    if not isinstance(raw_rows, list) or (not raw_rows and not allow_empty):
        raise ValueError('Add at least one team row.')
    with s.LOCK:
        if _final_started(s) or _running(s):
            raise ValueError('Finish or pause judging before changing the roster; a started Final locks team membership.')
        rows = [_normalise_row(s, item, c) for item in raw_rows]
        ids = [row['pair'] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError('Each team number must appear once.')
        scored = {row['pair'] for row in s.DB.execute("SELECT DISTINCT pair FROM sheets WHERE kind IN ('round','final') AND voided=0")}
        if scored - set(ids):
            raise ValueError('Keep teams with accepted scoring sheets in the roster.')
        with event_access.auth_db(s), s.DB:
            _invalidate_prepared_final(s)
            reconcile_attendees(s, rows)
            event_access.reconcile_participants(s, rows)
            participant_workspace.reconcile(s, rows)
            s.DB.execute('DELETE FROM roster_members')
            s.DB.execute('DELETE FROM roster')
            for row in rows:
                ms = {item['id']: item for item in row['members']}
                s.DB.execute('INSERT INTO roster(pair,partner,p1,p2,coach1,coach2) VALUES(?,?,?,?,?,?)',
                             (row['pair'], row['partner'], ms.get(1, {}).get('name', ''), ms.get(2, {}).get('name', ''),
                              ms.get(1, {}).get('coach', 0), ms.get(2, {}).get('coach', 0)))
                s.DB.executemany('INSERT INTO roster_members(pair,member,name,coach) VALUES(?,?,?,?)',
                                 [(row['pair'], member['id'], member['name'], member['coach']) for member in row['members']])
    return len(rows)


def _admin(s, h):
    import event_access
    ident = event_access.session(s, h.headers)
    if not ident and not s.hosted() and s.who(h.headers):
        role, _, _ = s.who(h.headers)
        ident = {'role': 'admin' if role == 'desk' else role}
    if not ident:
        h.send(401, {'error': 'Sign in to continue.'})
        return False
    if ident['role'] != 'admin':
        h.send(403, {'error': 'Admin access is required.'})
        return False
    return True


def handle_get(s, h, path, q):
    if path != '/api/admin/configuration':
        return False
    if _admin(s, h):
        h.send(200, status(s))
    return True


def handle_post(s, h, path, body):
    if path not in ('/api/admin/configuration', '/api/admin/configuration/preview'):
        return False
    if not _admin(s, h):
        return True
    import rehearsal_reconcile

    try:
        values = _validate(body)
        if path.endswith('/preview'):
            result = rehearsal_reconcile.preview(s, values)
            h.send(200, result)
            return True
        if body.get('confirm') == 'RESET REHEARSAL' and s.WORKSPACE and s.WORKSPACE.mode == 'rehearsal':
            s.WORKSPACE.reset('RESET REHEARSAL', configuration=values)
            result = status(s)
        elif 'plan_digest' in body:
            result = rehearsal_reconcile.apply(s, values, body['plan_digest'])
        else:
            result = update(s, values)
        if not result.get('replayed'):
            s.audit('admin', 'event-configuration', json.dumps({key: values[key] for key in LIMITS}, sort_keys=True))
        h.send(200, result)
    except rehearsal_reconcile.ResizeConflict as error:
        h.send(409, {'code': error.code, 'error': str(error)})
    except ValueError as error:
        h.send(400, {'error': str(error)})
    return True
