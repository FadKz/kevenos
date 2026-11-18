"""Private participant work, announcements, and reviewed score releases."""
import hashlib
import json
import uuid

STEPS = ('discover', 'mac', 'solve', 'close')
PARTICIPANT = '/api/participant/workspace'
DESK = '/api/desk/participant-workspace'


class Conflict(ValueError):
    pass


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS participant_teams (
        id TEXT PRIMARY KEY, pair INTEGER NOT NULL, identity TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, steps TEXT NOT NULL DEFAULT '{}',
        nomination TEXT NOT NULL DEFAULT '{}', preliminary TEXT)""")
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS participant_active_team ON participant_teams(pair) WHERE active=1')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(sheets)')}
    if 'participant_feedback_approved' not in columns:
        db.execute('ALTER TABLE sheets ADD COLUMN participant_feedback_approved INTEGER NOT NULL DEFAULT 0')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def identity(row):
    names = sorted((member['id'], member['name'].strip()) for member in row['members'] if member['name'].strip())
    return digest([row['partner'], names])


def reconcile(s, rows):
    """Retire private work before a team number can be assigned to other people."""
    incoming = {row['pair']: identity(row) for row in rows}
    for row in s.event_config.list_roster(s):
        if incoming.get(row['pair']) != identity(row):
            s.DB.execute("UPDATE sheets SET participant_feedback_approved=0 WHERE kind='round' AND pair=?",
                         (row['pair'],))
    for row in s.DB.execute('SELECT id,pair,identity FROM participant_teams WHERE active=1').fetchall():
        if incoming.get(row['pair']) != row['identity']:
            s.DB.execute('UPDATE participant_teams SET active=0 WHERE id=?', (row['id'],))


def team_record(s, row, create=False):
    fingerprint = identity(row)
    existing = s.DB.execute('SELECT * FROM participant_teams WHERE pair=? AND active=1', (row['pair'],)).fetchone()
    if existing and existing['identity'] == fingerprint:
        return dict(existing)
    # The first read is side-effect free. Serialize first writes under the app lock.
    team_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{s.WORKSPACE.context if s.WORKSPACE else s.DATA}:{row["pair"]}:{fingerprint}'))
    if s.DB.execute('SELECT 1 FROM participant_teams WHERE id=?', (team_id,)).fetchone():
        generations = s.DB.execute('SELECT count(*) FROM participant_teams WHERE pair=?', (row['pair'],)).fetchone()[0]
        team_id = str(uuid.uuid5(uuid.NAMESPACE_URL, team_id + ':' + str(generations)))
    record = {'id': team_id, 'steps': '{}', 'nomination': '{}', 'preliminary': None}
    if create:
        with s.DB:
            if existing:
                s.DB.execute('UPDATE participant_teams SET active=0 WHERE id=?', (existing['id'],))
            s.DB.execute('INSERT INTO participant_teams(id,pair,identity) VALUES(?,?,?)',
                         (team_id, row['pair'], fingerprint))
    return record


def nomination(record):
    return {'revision': 0, 'companies': [], 'submitted_at': None} | json.loads(record['nomination'])


def nomination_status(s):
    override = s.kv_get('participant_nominations')
    if override is not None:
        return override
    agenda = s.agenda()
    awards = next((index for index, item in enumerate(agenda) if item.get('scene') == 'awards'), None)
    return awards is not None and s.program_now()['i'] >= awards


def current_news(s):
    return [item for item in s.kv_get('participant_news', [])
            if not item.get('expired')]


def score_snapshot(s, row, sheet):
    groups = s.group_counts(sheet['ticks'])
    return {
        'total': s.sheet_score(sheet) + row['badge'],
        'groups': [count * 10 for count in groups],
        'bonus': (2 if sheet['ai'] else 0) + row['badge'],
        'feedback': sheet['feedback'].strip()
    }


def score_source(row, sheet):
    return digest([identity(row), row['badge'], {key: sheet[key] for key in (
        'id', 'ticks', 'ai', 'wrap_rating', 'spoke', 'feedback', 'participant_feedback_approved')}])


def preliminary(s, row, record):
    if not record['preliminary']:
        return None
    saved = json.loads(record['preliminary'])
    active = s.DB.execute("SELECT * FROM sheets WHERE kind='round' AND pair=? AND voided=0", (row['pair'],)).fetchall()
    if len(active) != 1 or score_source(row, s.sheet_dict(active[0])) != saved['source']:
        return {'status': 'corrected'}
    return saved['result'] | {'status': 'released'}


def review(s, roster):
    sheets_by_team = {}
    for sheet in s.active_sheets('round'):
        sheets_by_team.setdefault(sheet['pair'], []).append(sheet)
    entries = []
    for row in roster:
        record = team_record(s, row)
        sheets = sheets_by_team.get(row['pair'], [])
        entry = {'pair': row['pair'], 'partner': row['partner'], 'status': 'awaiting'}
        if len(sheets) == 1:
            sheet = sheets[0]
            entry.update(status='needs_approval')
            if sheet['participant_feedback_approved'] and sheet['feedback'].strip():
                published = preliminary(s, row, record) or {}
                status = 'released' if published.get('status') == 'released' else 'ready'
                entry.update(status=status, result=score_snapshot(s, row, sheet),
                             source=score_source(row, sheet))
        elif sheets:
            entry['status'] = 'duplicate'
        entries.append(entry)
    agenda = s.agenda()
    pitch = [i for i, item in enumerate(agenda) if item.get('scene') == 'pitch']
    after_pitch = bool(pitch and s.program_now()['i'] > max(pitch))
    in_final = s.final_event.engaged(s.final_event.get(s))
    clocks = [s.clock_public(station) for station in range(1, s.CFG['stations'] + 1)]
    active_round = any(clock['started_any'] and not clock['ended'] and clock['phase'] in ('pitch', 'feedback') for clock in clocks)
    ready = (after_pitch or in_final) and (in_final or not active_round)
    return {'entries': entries, 'digest': digest([entries, ready]), 'can_release': ready}


def participant_data(s, row):
    record = team_record(s, row)
    news = sorted(current_news(s), key=lambda item: (item['pinned'], item['at']), reverse=True)
    return {'team': record['id'], 'steps': json.loads(record['steps']), 'news': news,
            'nomination': nomination(record) | {'open': nomination_status(s)},
            'preliminary': preliminary(s, row, record)}


def cleaned(value, limit, label, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{label} must be {"provided and " if required else ""}at most {limit} characters.')
    return value.strip()


def revision(body, current):
    if type(body.get('revision')) is not int or body['revision'] != current:
        raise Conflict('Your teammate saved changes. Review their version before saving yours.')


def participant_write(s, row, body):
    record = team_record(s, row)
    if body.get('team') != record['id']:
        raise Conflict('Your team changed. Reload your assignment before saving.')
    action = body.get('action')
    if action == 'save_step':
        step = body.get('step')
        if step not in STEPS:
            raise ValueError('Choose a pitch step.')
        steps = json.loads(record['steps'])
        prior = steps.get(step, {}).get('revision', 0)
        revision(body, prior)
        steps[step] = {'revision': prior + 1, 'cues': cleaned(body.get('cues'), 1800, 'Speaking cues'),
                       'speaker': cleaned(body.get('speaker'), 120, 'Speaker')}
        team_record(s, row, create=True)
        with s.DB:
            s.DB.execute('UPDATE participant_teams SET steps=? WHERE id=?', (json.dumps(steps), record['id']))
        return {'step': steps[step]}
    if action in ('draft_nominations', 'submit_nominations'):
        if not nomination_status(s):
            raise Conflict('The Desk has not opened nominations, or has closed them.')
        previous = nomination(record)
        revision(body, previous['revision'])
        submitted = action == 'submit_nominations'
        values = body.get('companies')
        if not isinstance(values, list) or len(values) != 3 or any(not isinstance(item, dict) for item in values):
            raise ValueError('Provide three company entries.')
        companies = [{'name': cleaned(item.get('name'), 120, 'Company name', submitted),
                      'why': cleaned(item.get('why'), 1200, 'Your reason', submitted)} for item in values]
        if submitted and len({item['name'].casefold() for item in companies}) != 3:
            raise ValueError('Nominate three different companies.')
        if previous['submitted_at'] and not submitted:
            raise Conflict('Submit all three companies to update your existing nomination.')
        saved = {'revision': previous['revision'] + 1, 'companies': companies,
                 'submitted_at': s.time.time() if submitted else None}
        team_record(s, row, create=True)
        with s.DB:
            s.DB.execute('UPDATE participant_teams SET nomination=? WHERE id=?', (json.dumps(saved), record['id']))
        if submitted:
            s.audit('participant', 'participant-nomination', f'Team {row["pair"]}')
        return {'nomination': saved | {'open': True}}
    raise ValueError('Choose a participant action.')


def desk_data(s, roster):
    nominated = []
    for row in roster:
        saved = nomination(team_record(s, row))
        nominated.append({'pair': row['pair'], 'partner': row['partner'],
                          'submitted_at': saved['submitted_at'],
                          'companies': saved['companies'] if saved['submitted_at'] else []})
    return {'review': review(s, roster), 'nominations_open': nomination_status(s),
            'news': current_news(s), 'nominations': nominated}


def desk_write(s, roster, body, actor):
    action = body.get('action')
    if action == 'release':
        proof = review(s, roster)
        if body.get('digest') != proof['digest']:
            raise Conflict('Scores changed. Review the latest scores before releasing.')
        if not proof['can_release']:
            raise Conflict('Finish preliminary judging and advance the programme before releasing feedback.')
        targets = {entry['pair']: entry for entry in proof['entries'] if entry['status'] == 'ready'}
        if not targets:
            raise Conflict('No new judge-approved results are ready for release.')
        records = [(team_record(s, row, create=True), targets[row['pair']]) for row in roster if row['pair'] in targets]
        with s.DB:
            for record, entry in records:
                result = entry['result'] | {'released_at': s.time.time()}
                snapshot = {'source': entry['source'], 'result': result}
                s.DB.execute('UPDATE participant_teams SET preliminary=? WHERE id=?', (json.dumps(snapshot), record['id']))
        s.audit(actor, 'participant-release', f'{len(targets)} team results')
        return {'released': len(targets)}
    if action == 'nominations' and type(body.get('open')) is bool:
        s.kv_set('participant_nominations', body['open'])
        s.audit(actor, 'participant-nominations-window', 'open' if body['open'] else 'closed')
        return {'ok': True}
    if action in ('post_news', 'edit_news', 'expire_news'):
        items = s.kv_get('participant_news', [])
        if action == 'post_news':
            item = {'id': str(uuid.uuid4()), 'at': s.time.time()}
            items.append(item)
        else:
            item = next((value for value in items if value['id'] == body.get('id') and not value.get('expired')), None)
            if not item:
                raise Conflict('That update is no longer active.')
        if action == 'expire_news':
            item['expired'] = True
        else:
            item.update(text=cleaned(body.get('text'), 500, 'Desk update', True),
                        pinned=body.get('pinned') is True, at=s.time.time())
        s.kv_set('participant_news', items[-100:])
        s.audit(actor, 'participant-' + action, item['id'])
        return {'ok': True}
    raise ValueError('Choose a Desk action.')


def handle(s, h, path, body=None):
    if path not in (PARTICIPANT, DESK):
        return False
    with s.LOCK:
        access = s.event_access.session(s, h.headers)
        role = access['role'] if access else (s.who(h.headers) or (None,))[0]
        if role is None:
            h.send(401, {'error': 'Sign in to continue.'})
            return True
        allowed = ('participant',) if path == PARTICIPANT else ('admin', 'desk')
        if role not in allowed:
            h.send(403, {'error': 'This workspace is not available to your role.'})
            return True
        try:
            if path == PARTICIPANT:
                assignment = s.event_access.participant_assignment(s, access)
                if not assignment or not assignment['name'].strip():
                    raise Conflict('Your assignment is unavailable. Ask the Event Desk for help.')
                row = s.roster_row(assignment['pair'])
                data = participant_data(s, row) if body is None else participant_write(s, row, body)
            else:
                roster = [row for row in s.event_config.list_roster(s) if any(member['name'].strip() for member in row['members'])]
                data = desk_data(s, roster) if body is None else desk_write(s, roster, body, role)
                if body is None and access and access.get('dashboard')=='organizer':
                    data.pop('review',None)
                if body is None and access and access.get('dashboard')=='scoring':
                    data={k:v for k,v in data.items() if k=='review'}
        except Conflict as error:
            h.send(409, {'error': str(error)})
        except ValueError as error:
            h.send(400, {'error': str(error)})
        else:
            h.send(200, data)
    return True
