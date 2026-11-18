"""Short-lived, parent-bound Admin views with one effective request identity."""
import hashlib
import hmac
import json
import secrets


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS access_profiles (
      digest TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, parent_digest TEXT NOT NULL,
      target TEXT NOT NULL, fingerprint TEXT NOT NULL, dataset TEXT NOT NULL,
      workspace TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('preview','support')),
      expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS access_profiles_parent ON access_profiles(parent_digest);
    ''')
    for table in ('audit', 'visitor_activity'):
        columns = {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}
        for name in ('actor_credential', 'actor_label', 'effective_credential', 'effective_role', 'profile_id', 'impersonation_mode'):
            if name not in columns:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + " TEXT NOT NULL DEFAULT ''")


def context(s, digest):
    return hmac.new(s.CFG['secret'].encode(), ('access-context:' + digest).encode(), hashlib.sha256).hexdigest()


def epoch(s):
    return s.WORKSPACE.context if s.WORKSPACE else 'local'


def destination(ident):
    if ident.get('dashboard') in ('home','organizer','scoring'):return '/'+ident['dashboard']
    return '/judge/%d' % ident['station'] if ident['role'] == 'judge' else {
        'organizer':'/organizer','scoring':'/scoring','admin': '/admin', 'desk': '/desk', 'registration': '/registration', 'participant': '/participant', 'guest': '/guest'}[ident['role']]


def draft_key(s, ident):
    value = [ident.get('credential_id'), ident['station'], ident.get('dataset'),
             (ident.get('impersonation') or {}).get('id'), epoch(s), ident.get('account_id'), ident.get('account_revision',0)]
    return hmac.new(s.CFG['secret'].encode(), ('draft-v7:' + json.dumps(value)).encode(), hashlib.sha256).hexdigest()[:32]


def target(s, body):
    """Validate target selectors against current records, including unissued people."""
    access = s.event_access
    dashboard=body.get('dashboard') or body.get('role')
    if dashboard=='desk':dashboard='organizer'
    role='desk' if dashboard in ('organizer','scoring') else dashboard
    if role not in ('guest', 'participant', 'judge', 'desk', 'admin', 'registration'):
        raise ValueError('Choose a valid profile role.')
    cid = body.get('credential_id') or ''
    if not isinstance(cid, str) or len(cid) > 128:
        raise ValueError('Choose a valid profile account.')
    credential = None
    if cid:
        credential = access.auth_db(s).execute('SELECT * FROM access_credentials WHERE id=? AND active=1', (cid,)).fetchone()
        if not credential:raise ValueError('This profile is no longer available.')
        gs=access.event_roles.grants(s,credential['account_id'])
        wanted=dashboard if dashboard!='desk' else 'scoring'
        grant=next((g for g in gs if g['role']==wanted),None)
        if not grant or (dashboard=='desk' and not any(g['role']=='organizer' for g in gs)):
            raise ValueError('This role is no longer assigned to this person.')
        credential=dict(credential,station=grant['station'],pair=grant['pair'],member=grant['member'])
    selectors = {name: body.get(name, credential[name] if credential else 0) for name in ('pair', 'member', 'station')}
    if any(type(value) is not int for value in selectors.values()):
        raise ValueError('Choose valid profile identifiers.')
    if credential and any(selectors[key] != credential[key] for key in selectors):
        raise ValueError('The profile identifiers do not match this account.')
    pair, member, station = (selectors[key] for key in ('pair', 'member', 'station'))
    label = credential['label'] if credential else {'guest': 'Event guests', 'desk': 'Organizer', 'admin': 'Admin', 'registration':'Registration'}.get(role, '')
    person = None
    if role == 'participant':
        person = s.event_config.roster_row(s, pair)
        if not person or not 1 <= member <= len(person['members']) or not person['members'][member-1]['name'].strip() or station:
            raise ValueError('Choose a named participant from this event.')
        label = person['members'][member-1]['name']
    elif role == 'judge':
        if pair or member or not 1 <= station <= s.CFG['stations']:
            raise ValueError('Choose a configured judging station.')
        if not credential:
            credential = access.auth_db(s).execute("SELECT * FROM access_credentials WHERE role='judge' AND active=1 AND station=? AND dataset=?", (station, access.active_dataset(s))).fetchone()
            label = credential['label'] if credential else 'Staff at station %d' % station
            cid = credential['id'] if credential else ''
    elif pair or member or station:
        raise ValueError('This role does not accept a station or participant.')
    # Even a person selected without a credential is invalidated when their
    # credential changes, or the roster/configuration changes their assignment.
    if role == 'participant' and not credential:
        credential = access.auth_db(s).execute("SELECT * FROM access_credentials WHERE role='participant' AND active=1 AND pair=? AND member=? AND dataset=?", (pair, member, access.active_dataset(s))).fetchone()
        cid = credential['id'] if credential else ''
    account=access.auth_db(s).execute('SELECT id,revision,active FROM person_accounts WHERE id=?',(credential['account_id'],)).fetchone() if credential else None
    if account and not account['active']:raise ValueError('This account is inactive.')
    out = {'dashboard':dashboard, 'account_id':account['id'] if account else None, 'account_revision':account['revision'] if account else 0, 'role': role, 'label': label, 'credential_id': cid, 'station': station,
           'pair': pair, 'member': member, 'dataset': access.credential_dataset(s, role)}
    proof = [out, credential['password_hash'] if credential else '', person,
             s.event_config.config(s, read_only=True)]
    fingerprint = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
    return out, fingerprint


def resolve(s, headers, parent):
    token = headers.get('X-Profile-Token')
    if not token:
        return parent
    if not parent or not any(g['role']=='admin' for g in parent.get('grants',[])) or len(token) != 43:
        return None
    row = s.event_access.auth_db(s).execute('SELECT * FROM access_profiles WHERE digest=? AND revoked=0 AND expires>?',
        (s.credential_digest(token), s.time.time())).fetchone()
    if not row or row['parent_digest'] != parent['digest'] or row['dataset'] != s.event_access.active_dataset(s) or row['workspace'] != epoch(s):
        return None
    try:
        current, fingerprint = target(s, json.loads(row['target']))
    except (ValueError, TypeError, KeyError):
        return None
    if fingerprint != row['fingerprint']:
        return None
    current.update(digest=parent['digest'], expires=min(parent['expires'], row['expires']),
                   judge=current['label'], context=context(s, row['digest']),
                   actor={'role': 'admin', 'credential_id': parent['credential_id'], 'label': parent['label']},
                   impersonation={'id': row['id'], 'mode': row['mode'], 'expires': row['expires']})
    if current['role'] == 'judge':
        current['draft_key'] = draft_key(s, current)
    return current


def targets(s):
    out = []
    def add(body):
        value, _ = target(s, body)
        opaque = context(s, 'target:' + s.event_access.active_dataset(s) + ':' + json.dumps(value, sort_keys=True))[:24]
        out.append(value | {'id': opaque, 'available': True})
    for role in ('guest', 'organizer', 'scoring', 'registration'):
        add({'role': role})
    for row in s.event_config.list_roster(s):
        for member in row['members']:
            if member['name'].strip():
                add({'role': 'participant', 'pair': row['pair'], 'member': member['id']})
    for station in range(1, s.CFG['stations'] + 1):
        add({'role': 'judge', 'station': station})
    return out


def guard(s, h, path, write=False):
    """Run before all domain handlers. A bad child never becomes its parent."""
    if not s.hosted():
        return False
    if path in ('/api/access/login', '/api/login', '/api/site-login', '/api/site-logout', '/api/role-logout'):
        return False
    if s.event_access.event_roles.guard(s,h,path):return True
    ident = s.event_access.session(s, h.headers)
    if h.headers.get('X-Profile-Token') and not ident:
        h.send(401, {'code': 'profile_expired', 'error': 'This profile session ended. Return to Admin to choose a profile again.'})
        return True
    marker = h.headers.get('X-Access-Context')
    if marker and (not ident or not s.match_secret(marker, ident['context'])):
        h.send(409, {'code': 'access_changed', 'error': 'Your sign-in or profile changed. Reload before continuing.'})
        return True
    if write and ident and path != '/api/access/logout' and not marker:
        h.send(409, {'code': 'access_changed', 'error': 'Refresh your sign-in before submitting changes.'})
        return True
    h._request_identity = ident
    s.REQUEST_AUTH.identity = ident
    if ident:
        h._response_access_context = ident['context']
    profile = (ident or {}).get('impersonation')
    if profile and path.startswith('/api/admin/'):
        h.send(403, {'error': 'Return to Admin before managing the event.'})
        return True
    if write and profile and profile['mode'] == 'preview' and path not in (
            '/api/access/impersonate', '/api/access/impersonate/end', '/api/access/logout',
            '/api/assistant/message', '/api/assistant/cancel', '/api/assistant/reset'):
        if path == '/api/sync' and ident['role'] == 'judge':
            h.send(200, s.judge_state(ident['station']))
        else:
            h.send(403, {'code': 'preview_read_only', 'error': 'This profile is a read-only preview. Enable support mode to make changes.'})
        return True
    return False


def handle_get(s, h, path):
    if path != '/api/admin/profiles':
        return False
    ident = s.event_access.session(s, h.headers)
    if not ident:
        h.send(401, {'error': 'Sign in to continue.'})
    elif ident['role'] != 'admin' or ident.get('impersonation'):
        h.send(403, {'error': 'Admin access is required.'})
    else:
        with s.LOCK:
            h.send(200, {'targets': targets(s)})
    return True


def handle_post(s, h, path, body):
    if path not in ('/api/access/impersonate', '/api/access/impersonate/end'):
        return False
    parent = s.event_access.root_session(s, h.headers)
    if not parent or not any(g['role']=='admin' for g in parent.get('grants',[])):
        h.send(403 if parent else 401, {'error': 'Admin access is required.'})
        return True
    old = h.headers.get('X-Profile-Token')
    effective=s.event_access.session(s,h.headers)
    if not old and (not effective or effective['role']!='admin'):
        h.send(403,{'error':'Open Event Administration to preview or support another profile.'});return True
    with s.LOCK, s.event_access.auth_db(s):
        if path.endswith('/end'):
            if old:
                s.event_access.auth_db(s).execute('UPDATE access_profiles SET revoked=1 WHERE digest=? AND parent_digest=?',
                                                 (s.credential_digest(old), parent['digest']))
            h._response_access_context = parent['context']
            s.audit('admin', 'profile-end')
            h.send(200, {'context': parent['context'], 'next': '/admin', 'identity': s.event_access.session_info(parent)})
            return True
        try:
            mode = body.get('mode', 'preview')
            if mode not in ('preview', 'support'):
                raise ValueError('Choose preview or support mode.')
            selected, fingerprint = target(s, body)
            if old:
                previous = s.event_access.session(s, h.headers)
                if not previous or any(selected[key] != previous[key] for key in ('role', 'dashboard', 'station', 'pair', 'member', 'credential_id')):
                    raise ValueError('Return to Admin before selecting a different profile.')
                s.event_access.auth_db(s).execute('UPDATE access_profiles SET revoked=1 WHERE digest=?', (s.credential_digest(old),))
            if selected['role'] == 'admin':
                raise ValueError('Use your Admin dashboard directly. Preview and Support apply to event-facing profiles.')
            token = secrets.token_urlsafe(32)
            digest = s.credential_digest(token)
            expires = min(parent['expires'], s.time.time() + 900)
            s.event_access.auth_db(s).execute('INSERT INTO access_profiles(digest,id,parent_digest,target,fingerprint,dataset,workspace,mode,expires) VALUES(?,?,?,?,?,?,?,?,?)',
                (digest, secrets.token_hex(12), parent['digest'], json.dumps(selected), fingerprint,
                 s.event_access.active_dataset(s), epoch(s), mode, expires))
            headers = dict(h.headers)
            headers['X-Profile-Token'] = token
            effective = resolve(s, headers, parent)
            h._response_access_context = effective['context']
            s.audit('admin', 'profile-start', json.dumps({'role': selected['role'], 'station': selected['station'],
                'pair': selected['pair'], 'member': selected['member'], 'mode': mode}))
            h.send(200, {'profile_token': token, 'context': effective['context'], 'next': destination(effective),
                        'identity': s.event_access.session_info(effective)})
        except (ValueError, TypeError, KeyError):
            h.send(400, {'error': 'Choose an available profile with matching identifiers and mode.'})
    return True
