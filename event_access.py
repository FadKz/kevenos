"""Scoped event access and first-party activity reporting; no provider dependencies."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import database
import time
from urllib.parse import unquote, urlparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import event_config
import participant_companion
import event_profiles
import event_roles

RETENTION_DAYS = 30
MAX_EVENTS = 50000
ROUTES = {'organizer':'/organizer','scoring':'/scoring','admin': '/admin', 'desk': '/desk', 'registration': '/registration', 'participant': '/participant', 'guest': '/guest', 'judge': '/judge'}
CREDENTIAL_FIELDS = 'id,label,role,pair,member,station,revision,active,created,last_used,dataset'
DESK_GET = {'/api/content','/api/desk/state','/api/desk/scores','/api/desk/sheets','/api/desk/calib','/api/desk/export.csv'}
DESK_POST = {'/api/desk/clock','/api/desk/mode','/api/desk/program','/api/desk/public','/api/desk/reveal','/api/desk/final','/api/desk/calib','/api/desk/sheet','/api/desk/ai-summary'}
PAGES = {'/home','/event','/organizer','/scoring','/','/index.html','/guest','/participant','/admin','/desk','/judge','/live','/board','/registration','/join'}


def migrate(db):
    cols = {r['name'] for r in db.execute('PRAGMA table_info(role_sessions)')}
    if 'credential_id' not in cols:
        db.execute("ALTER TABLE role_sessions ADD COLUMN credential_id TEXT NOT NULL DEFAULT ''")
    if 'legacy_gate' not in cols:
        db.execute('ALTER TABLE role_sessions ADD COLUMN legacy_gate INTEGER NOT NULL DEFAULT 1')
        db.execute("UPDATE role_sessions SET role='admin' WHERE role='desk'")
    db.executescript('''
    CREATE TABLE IF NOT EXISTS access_credentials (
      id TEXT PRIMARY KEY, label TEXT NOT NULL, role TEXT NOT NULL,
      pair INTEGER NOT NULL DEFAULT 0, member INTEGER NOT NULL DEFAULT 0,
      lookup TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, last_used REAL);
    CREATE TABLE IF NOT EXISTS visitor_activity (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, visitor_id TEXT NOT NULL,
      credential_id TEXT NOT NULL, label TEXT NOT NULL, role TEXT NOT NULL,
      ip TEXT NOT NULL, country TEXT NOT NULL, region TEXT NOT NULL, city TEXT NOT NULL,
      action TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS activity_time ON visitor_activity(ts);
    CREATE INDEX IF NOT EXISTS activity_role ON visitor_activity(role,id);
    ''')


    for table in ('access_credentials', 'role_sessions', 'visitor_activity'):
        columns = {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
        if 'dataset' not in columns:
            default = 'live' if table == 'visitor_activity' else 'global'
            db.execute("ALTER TABLE "+table+" ADD COLUMN dataset TEXT NOT NULL DEFAULT '"+default+"'")
            if table != 'visitor_activity':
                db.execute("UPDATE "+table+" SET dataset='live' WHERE role IN ('participant','judge')")
    db.execute('DROP INDEX IF EXISTS participant_access')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS participant_access ON access_credentials(dataset,pair,member) WHERE role='participant' AND active=1")
    columns = {row['name'] for row in db.execute('PRAGMA table_info(access_credentials)')}
    for name, default in (('station', 0), ('revision', 1)):
        if name not in columns:
            db.execute('ALTER TABLE access_credentials ADD COLUMN ' + name + ' INTEGER NOT NULL DEFAULT ' + str(default))
    if 'password_box' not in columns:
        db.execute('ALTER TABLE access_credentials ADD COLUMN password_box BLOB')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS judge_password_access ON access_credentials(dataset,station) WHERE role='judge' AND active=1")
    event_profiles.migrate(db)
    event_roles.migrate(db)


def auth_db(s):
    return getattr(s, 'AUTH_DB', None) or s.DB


def active_dataset(s):
    workspace = getattr(s, 'WORKSPACE', None)
    return workspace.mode if workspace else 'live'


def credential_dataset(s, role):
    return active_dataset(s) if role in ('participant','judge') else 'global'


def lookup(s, password):
    return hmac.new(s.CFG['secret'].encode(), ('access-password:'+password).encode(), hashlib.sha256).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return salt+':'+digest.hex()


def password_cipher(s):
    key = hmac.digest(s.CFG['secret'].encode(), b'kevenos:credential-reveal:v1', 'sha256')
    return AESGCM(key)


def password_context(cid, revision, dataset):
    return json.dumps([cid, revision, dataset], separators=(',', ':')).encode()


def seal_password(s, password, cid, revision, dataset):
    nonce = secrets.token_bytes(12)
    return nonce + password_cipher(s).encrypt(nonce, password.encode(), password_context(cid, revision, dataset))


def reveal_password(s, credential):
    box = credential['password_box']
    if box is None:
        raise ValueError('This password predates password reveal. Ask the account holder to sign in once with their current password.')
    try:
        return password_cipher(s).decrypt(box[:12], box[12:], password_context(
            credential['id'], credential['revision'], credential['dataset'])).decode()
    except (InvalidTag, ValueError) as error:
        raise ValueError('This password could not be opened. Ask Admin to check the credential storage.') from error


def seed_guest(s):
    # Seed once. Subsequent Admin resets take precedence over the bootstrap mount.
    if not s.CFG.get('guest_password'):
        return
    db = auth_db(s)
    with s.LOCK:
        if db.execute("SELECT 1 FROM access_credentials WHERE id='guest'").fetchone():
            return
    with s.LOCK, database.named_lock(db, 'demo-access-seed'), db:
        if not db.execute("SELECT 1 FROM access_credentials WHERE id='guest'").fetchone():
            create_credential(s, 'guest', 'Event guests', password=s.CFG['guest_password'], cid='guest', demo=_demo_passwords())


def _demo_passwords():
    return os.environ.get('KEVENOS_DEMO_ROLE_PASSWORDS') == '1'


def _demo_roles(s):
    return (
        ('organizer', 'Demo Organizer', {}),
        ('scoring', 'Demo Scoring Desk', {}),
        ('registration', 'Demo Registration Desk', {}),
        ('judge', 'Demo Judge', {'station': 1}),
        ('participant', 'Demo Participant', {'pair': 1, 'member': 1}),
    )


def _demo_roles_current(s, roles):
    db = auth_db(s)
    role_ids = tuple('demo-' + role for role, _, _ in roles)
    placeholders = ','.join('?' for _ in role_ids)
    rows = db.execute(
        '''SELECT c.id,c.label,c.role,c.pair,c.member,c.station,c.dataset,c.lookup,
                  c.active,c.account_id,a.label AS account_label,a.active AS account_active,
                  g.station AS grant_station,g.pair AS grant_pair,g.member AS grant_member,
                  g.subject AS grant_subject
             FROM access_credentials c
             LEFT JOIN person_accounts a ON a.id=c.account_id
             LEFT JOIN account_grants g ON g.account_id=c.account_id
                  AND g.role=c.role AND g.dataset=c.dataset
            WHERE c.id IN (''' + placeholders + ')',
        role_ids,
    ).fetchall()
    existing_by_id = {row['id']: row for row in rows}
    for role, label, fields in roles:
        cid = 'demo-' + role
        existing = existing_by_id.get(cid)
        dataset = credential_dataset(s, role)
        pair, member, station = fields.get('pair', 0), fields.get('member', 0), fields.get('station', 0)
        if role == 'participant':
            roster = event_config.roster_row(s, pair)
            label = roster['members'][member - 1]['name'] if roster else ''
        if not existing or not existing['active'] or not existing['account_id']:
            return False
        if (existing['label'], existing['role'], existing['dataset'], existing['station'], existing['pair'], existing['member']) != (
                label, role, dataset, station, pair, member):
            return False
        if not s.match_secret(existing['lookup'], lookup(s, role)):
            return False
        if not existing['account_active'] or existing['account_label'] != label or existing['grant_station'] is None:
            return False
        if (existing['grant_station'], existing['grant_pair'], existing['grant_member'], existing['grant_subject']) != (
                station, pair, member, label if role == 'participant' else ''):
            return False
    return True


def create_credential(s, role, label, pair=0, member=0, password=None, cid=None, station=0, revision=1, demo=False):
    if role not in ROUTES:
        raise ValueError('Choose a valid access role.')
    label = str(label or '').strip()
    if not 1 <= len(label) <= 80:
        raise ValueError('Enter a name or label (up to 80 characters).')
    if role == 'participant':
        configuration = event_config.config(s)
        if type(pair) is not int or not 1 <= pair <= configuration['team_count'] or type(member) is not int or not 1 <= member <= event_config.member_capacity(configuration, pair):
            raise ValueError('Choose a participant from the roster.')
        row = event_config.roster_row(s, pair)
        name = row['members'][member-1]['name'] if row else ''
        if not name.strip():
            raise ValueError('Add the participant’s name to the roster first.')
        label = name
    else:
        pair = member = 0
    if role == 'judge':
        if type(station) is not int or not 1 <= station <= s.CFG['stations']:
            raise ValueError('Choose a configured judging station.')
    else:
        station = 0
    password = password if password is not None else secrets.token_urlsafe(12)
    simple_demo = demo and _demo_passwords() and password == role
    if not isinstance(password,str) or not simple_demo and not (8 if role=='guest' else 10) <= len(password) <= 128:
        raise ValueError('Use a password of 8–128 characters for guests or 10–128 for individual access.')
    if s.match_secret(password,s.CFG['password']):
        raise ValueError('Choose a different password from the owner recovery password.')
    if role == 'guest':
        cid = 'guest'
    cid = cid or secrets.token_hex(12)
    dataset = credential_dataset(s, role)
    event_roles.validate_unique(s,dict(role=role,dataset=dataset,station=station,pair=pair,member=member),cid)
    auth_db(s).execute('INSERT INTO access_credentials(id,label,role,pair,member,station,revision,lookup,password_hash,created,dataset,password_box) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                 (cid,label,role,pair,member,station,revision,lookup(s,password),password_hash(password),time.time(),dataset,
                  seal_password(s, password, cid, revision, dataset)))
    event_roles.ensure_account(auth_db(s), auth_db(s).execute('SELECT * FROM access_credentials WHERE id=?',(cid,)).fetchone())
    row = dict(auth_db(s).execute('SELECT '+CREDENTIAL_FIELDS+' FROM access_credentials WHERE id=?',(cid,)).fetchone())
    return {'credential':row,'password':password}


def seed_demo_roles(s):
    """Create documented synthetic role logins only in the disposable demo."""
    if not _demo_passwords():
        return
    roles = _demo_roles(s)
    db = auth_db(s)
    with s.LOCK:
        if _demo_roles_current(s, roles):
            return
    with s.LOCK, database.named_lock(db, 'demo-access-seed'), db:
        if _demo_roles_current(s, roles):
            return
        for role, label, fields in roles:
            cid = 'demo-' + role
            existing = db.execute('SELECT * FROM access_credentials WHERE id=?', (cid,)).fetchone()
            if not existing:
                create_credential(s, role, label, password=role, cid=cid, demo=True, **fields)
                continue
            dataset = credential_dataset(s, role)
            pair, member, station = fields.get('pair', 0), fields.get('member', 0), fields.get('station', 0)
            if role == 'participant':
                roster = event_config.roster_row(s, pair)
                label = roster['members'][member - 1]['name']
            desired = dict(role=role, dataset=dataset, station=station, pair=pair, member=member)
            aid = event_roles.ensure_account(db, existing)
            event_roles.validate_unique(s, desired, aid)
            digest_ok = s.match_secret(existing['lookup'], lookup(s, role)) and s.match_secret(
                existing['password_hash'], password_hash(role, existing['password_hash'].split(':', 1)[0]))
            revision = existing['revision'] + (0 if digest_ok else 1)
            password_box = existing['password_box'] if digest_ok else seal_password(s, role, cid, revision, dataset)
            db.execute('UPDATE access_credentials SET label=?,role=?,pair=?,member=?,station=?,dataset=?,active=1,lookup=?,password_hash=?,password_box=?,revision=? WHERE id=?',
                       (label, role, pair, member, station, dataset, lookup(s, role),
                        existing['password_hash'] if digest_ok else password_hash(role), password_box, revision, cid))
            db.execute('UPDATE person_accounts SET label=?,active=1 WHERE id=?', (label, aid))
            db.execute('DELETE FROM account_grants WHERE account_id=? AND role=?', (aid, role))
            db.execute('INSERT INTO account_grants VALUES(?,?,?,?,?,?,?)',
                       (aid, role, dataset, station, pair, member, label if role == 'participant' else ''))
            if not digest_ok:
                event_roles.revoke_sessions(db, aid)



def credential_view(s, credential):
    row = dict(credential)
    stored = auth_db(s).execute('SELECT account_id FROM access_credentials WHERE id=?', (row['id'],)).fetchone()
    aid = stored['account_id'] if stored else row['id']
    grants = event_roles.grants(s, aid)
    shared = auth_db(s).execute("SELECT 1 FROM account_grants WHERE account_id=? AND dataset!='rehearsal' LIMIT 1",(aid,)).fetchone()
    can_manage = bool(grants) and (active_dataset(s) != 'rehearsal' or shared is None)
    return row | {'account_id': aid, 'account_type': event_roles.account_type(s, aid), 'grants': grants, 'can_manage_password': can_manage}


def create_staff(s, body, ident):
    if set(body) - {'action', 'label', 'password', 'grants', 'confirm_shared_roles'}:
        raise ValueError('Provide a staff name, password and role assignments.')
    values = body.get('grants')
    if not isinstance(values, list) or not values or any(not isinstance(g, dict) or not isinstance(g.get('role'), str) or g.get('role') not in event_roles.STAFF or set(g) - {'role', 'station'} for g in values):
        raise ValueError('Choose at least one staff role. Participant accounts come from the roster.')
    assignments = event_roles.validated(s, body)
    for grant in assignments:
        event_roles.validate_unique(s, grant)
    shared = any(g['dataset'] == 'global' for g in assignments)
    if active_dataset(s) == 'rehearsal' and shared and body.get('confirm_shared_roles') is not True:
        raise ValueError('Confirm that shared staff roles apply in Live and Rehearsal.')
    # The original credential role remains for old clients; grants are the
    # authority. Prefer a shared role when this account has shared duties.
    initial = next((g for g in assignments if g['dataset'] == 'global'), assignments[0])
    result = create_credential(s, initial['role'], body.get('label'), password=body.get('password'), station=initial['station'])
    aid = result['credential']['id']
    db = auth_db(s)
    db.execute('DELETE FROM account_grants WHERE account_id=?', (aid,))
    for g in assignments:
        db.execute('INSERT INTO account_grants VALUES(?,?,?,?,?,?,?)', (aid,g['role'],g['dataset'],g['station'],g['pair'],g['member'],g['subject']))
    # Persist a compact receipt in shared auth storage even during rehearsal.
    db.execute('INSERT INTO audit(ts,actor,action,detail,actor_credential,actor_label,effective_credential,effective_role,profile_id,impersonation_mode) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (time.time(),'admin','staff-created',json.dumps({'account_id':aid,'roles':[g['role'] for g in assignments],
                'dataset':active_dataset(s),'shared_roles_confirmed':shared and active_dataset(s)=='rehearsal'}),
                ident.get('credential_id',''),ident.get('label',''),ident.get('credential_id',''),'admin','',''))
    return result


def root_session(s, headers):
    # Hosted identity is cookie-only. An old station bearer must never override
    # the browser's current sign-in or create a second hosted login mechanism.
    token = s.cookie_of(headers,'pp_access') if s.hosted() else headers.get('X-Token') or s.cookie_of(headers,'pp_access')
    if not token or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token) or auth_db(s) is None:
        return None
    with s.LOCK:
        r = auth_db(s).execute('SELECT * FROM role_sessions WHERE digest=? AND expires>?',(s.credential_digest(token),time.time())).fetchone()
        if not r or (r['assurance']=='participant_qr' and r['dataset'] != active_dataset(s)) or (s.hosted() and r['legacy_gate']) or (r['legacy_gate'] and r['role'] in ('admin','desk') and not s.site_ok(headers)):
            return None
        out = dict(r)
        out.update(label=r['judge'] or ('Owner' if r['role']=='admin' else r['role'].title()),pair=0,member=0)
        c = None
        if r['credential_id'] and r['credential_id'] != 'owner-recovery':
            c = auth_db(s).execute('SELECT * FROM access_credentials WHERE id=? AND active=1',(r['credential_id'],)).fetchone()
            if not c:
                return None
            out.update(label=c['label'],pair=c['pair'],member=c['member'])
        out = event_roles.enrich(s,out,c)
        if not out: return None
        out.update(context=event_profiles.context(s, out['digest']+':'+str(out['account_revision'])), actor=None, impersonation=None)
        if out['role'] == 'judge':
            out['draft_key'] = event_profiles.draft_key(s, out)
        return out


def session(s, headers):
    with s.LOCK:
        parent=root_session(s,headers)
        if headers.get('X-Profile-Token'): return event_profiles.resolve(s,headers,parent)
        return event_roles.select(s,headers,parent) if s.hosted() else parent


def session_info(r):
    if not r:
        return {'authenticated':False}
    return {'authenticated':True,'role':r['role'],'label':r['label'],'pair':r['pair'],'member':r['member'],
            'station':r['station'],'context':r.get('context',''),'actor':r.get('actor'),
            'impersonation':r.get('impersonation'),'next':event_profiles.destination(r),
            'account_id':r.get('account_id'),'account_label':r.get('account_label',r['label']),
            'dashboard':r.get('dashboard',r['role']),'dashboards':event_roles.dashboards(r),
            'account_revision':r.get('account_revision'),
            **({'draft_key':r['draft_key']} if r.get('draft_key') else {})}


def mint_session(s, role, cid, label, station=0, assurance="password"):
    token = secrets.token_urlsafe(32)
    c=auth_db(s).execute('SELECT account_id FROM access_credentials WHERE id=?',(cid,)).fetchone()
    staff=bool(c and auth_db(s).execute("SELECT 1 FROM account_grants WHERE account_id=? AND role IN ('admin','organizer','scoring','registration')",(c['account_id'],)).fetchone())
    ttl = s.ORGANISER_TTL if assurance=='password' and (staff or role in ('admin','desk','registration')) else 24*3600
    auth_db(s).execute('DELETE FROM role_sessions WHERE expires<=?',(time.time(),))
    auth_db(s).execute('INSERT INTO role_sessions(digest,role,station,judge,expires,credential_id,legacy_gate,dataset) VALUES(?,?,?,?,?,?,0,?)',
                 (s.credential_digest(token),role,station,label,time.time()+ttl,cid,credential_dataset(s,role)))
    account=auth_db(s).execute('SELECT a.revision FROM person_accounts a JOIN access_credentials c ON c.account_id=a.id WHERE c.id=?',(cid,)).fetchone()
    auth_db(s).execute('UPDATE role_sessions SET assurance=?,account_revision=? WHERE digest=?',(assurance,account['revision'] if account else 1,s.credential_digest(token)))
    return token,ttl


def access_cookie(s, value, ttl):
    return 'pp_access='+value+'; Path=/; Max-Age='+str(ttl)+'; HttpOnly; SameSite=Lax'+('; Secure' if s.hosted() else '')


def client_location(s,h):
    ip=h.client_address[0]
    geo={'country':'','region':'','city':''}
    try:
        peer=ipaddress.ip_address(ip)
        trusted=os.environ.get('KEVENOS_TRUST_CF_HEADERS')=='1' and (peer.is_private or peer.is_loopback) and h.headers.get('CF-Ray')
        if trusted:
            ip=str(ipaddress.ip_address(h.headers.get('CF-Connecting-IP','')))
            country=h.headers.get('CF-IPCountry','').upper()
            geo['country']=country if re.fullmatch('[A-Z]{2}',country) else ''
            if os.environ.get('KEVENOS_CF_LOCATION_HEADERS')=='1':
                for key,header in (('city','CF-IPCity'),('region','CF-Region')):
                    geo[key]=''.join(c for c in unquote(h.headers.get(header,'')) if c.isprintable())[:80]
    except ValueError:
        ip=h.client_address[0]
    return ip,geo


def same_origin(h):
    origin=h.headers.get('Origin')
    if h.headers.get('Sec-Fetch-Site')=='cross-site':
        return False
    if origin:
        parsed=urlparse(origin)
        return parsed.scheme in ('http','https') and parsed.netloc.lower()==h.headers.get('Host','').lower()
    return True


def clean_path(path):
    path=urlparse(path).path.rstrip('/') or '/'
    if path in PAGES or re.fullmatch(r'/judge/[1-8]',path):
        return path
    if path in ('/api/registration', '/api/access/registration-link'):
        return path
    if re.fullmatch(r'/api/(?:access/(?:login|logout|impersonate(?:/end)?)|site-login|site-logout|login|role-logout|submit|session|ai/(?:feedback|tidy)|activity|admin/(?:credentials(?:/share)?|profiles|activity|workspace|notifications(?:/check)?)|desk/[a-z.-]+|resources/[a-z0-9-]+)',path):
        return path
    return '/unknown'


def prune(s):
    auth_db(s).execute('DELETE FROM visitor_activity WHERE ts<?',(time.time()-RETENTION_DAYS*86400,))
    auth_db(s).execute('DELETE FROM visitor_activity WHERE id <= COALESCE((SELECT id FROM visitor_activity ORDER BY id DESC LIMIT 1 OFFSET ?),0)',(MAX_EVENTS,))


def track_response(s,h,status):
    """Record navigation, downloads, sign-ins and mutations, excluding poll traffic and payloads."""
    if auth_db(s) is None or getattr(h,'_activity_done',False):
        return None
    h._activity_done=True
    path=clean_path(h.path)
    is_page=path in PAGES or bool(re.fullmatch(r'/judge/[1-8]',path))
    if not (is_page or path.startswith('/api/resources/') or (h.command=='POST' and path not in ('/unknown','/api/sync'))):
        return None
    if h.command=='HEAD':
        return None
    raw=s.cookie_of(h.headers,'pp_visitor')
    parts=raw.split('.')
    vid=parts[0] if len(parts)==2 and re.fullmatch(r'[A-Za-z0-9_-]{22}',parts[0]) else ''
    def signature(v): return hmac.new(s.CFG['secret'].encode(),('visitor:'+v).encode(),hashlib.sha256).hexdigest()[:24]
    valid=bool(vid) and s.match_secret(parts[1],signature(vid))
    if not valid: vid=secrets.token_urlsafe(16)
    ident=getattr(h,'_activity_identity',None) or getattr(h,'_request_identity',None) or session(s,h.headers) or {}
    role=ident.get('role','anonymous')
    credential=ident.get('credential_id','')
    if role=='judge' and not credential: credential='judge-station-'+str(ident.get('station',0))
    action='view' if is_page else ('download' if path.startswith('/api/resources/') else 'action')
    if path in ('/api/access/login','/api/login','/api/site-login'):
        action='signin' if status==200 else 'signin-failed'
    elif path in ('/api/access/logout','/api/role-logout','/api/site-logout'): action='signout'
    action=getattr(h,'_activity_action',action)
    path=getattr(h,'_activity_path',path)
    ip,geo=client_location(s,h)
    actor = ident.get('actor') or ident
    with s.LOCK, auth_db(s):
        auth_db(s).execute('INSERT INTO visitor_activity(ts,visitor_id,credential_id,label,role,ip,country,region,city,action,path,status,dataset,actor_credential,actor_label,effective_credential,effective_role,profile_id,impersonation_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (time.time(),vid,credential,ident.get('label',ident.get('judge','')),role,ip,geo['country'],geo['region'],geo['city'],action,path,status,active_dataset(s),
                      actor.get('credential_id',''),actor.get('label',''),credential,role,(ident.get('impersonation') or {}).get('id',''),(ident.get('impersonation') or {}).get('mode','')))
        prune(s)
    if not valid:
        return 'pp_visitor='+vid+'.'+signature(vid)+'; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax'+('; Secure' if s.hosted() else '')
    return None


def reconcile_participants(s, rows):
    """A replaced roster person must never inherit the previous person's password."""
    incoming = {}
    for row in rows:
        if isinstance(row, dict):
            for member in row['members']:
                incoming[(row['pair'], member['id'])] = member['name']
        else:
            incoming.update({(row[0], member): row[2 if member == 1 else 3] for member in (1, 2)})
    event_roles.reconcile_participants(s,incoming)
    if auth_db(s) is not s.DB:
        auth_db(s).commit()


def guide_data(s):
    p=Path(s.HERE)/'event-guide.json'
    guide=json.loads(p.read_text()) if p.exists() else {'resources':[]}
    event=s.CONTENT['event']
    guide.update(title=event['name'],date=event.get('date',''),venue=event.get('venue',''))
    guide['content_version'] = s.CONTENT.get('content_version', 1)
    guide['schedule']=[{'time':a.get('time',''),'title':a.get('title',''),'detail':a.get('line',''),
                        'min':a.get('min',0)} for a in s.agenda()]
    configuration = event_config.config(s, read_only=True)
    guide['configuration'] = configuration
    guide['pitch_block_minutes'] = configuration['pitch_block_minutes']
    guide['schedule_warnings'] = event_config.schedule_warnings(s, configuration)
    return guide


def participant_assignment(s,ident):
    pair=ident.get('pair',0)
    if ident['role']!='participant' or not pair:
        return None
    row=event_config.roster_row(s, pair)
    if not row or not 1 <= ident['member'] <= len(row['members']):
        return None
    teammates = [member['name'] for member in row['members'] if member['id'] != ident['member'] and member['name'].strip()]
    return {'name':row['members'][ident['member']-1]['name'], 'pair':pair,'partner':row['partner'],
            'teammate':', '.join(teammates), 'teammates':teammates, 'team_size':len(row['members']),
            'station':row['station'], 'round':row['round'],'scenario':row['scenario']}


def resource_allowed(s,resource,ident):
    if not ident: return False
    role=ident['role']
    if role=='admin': return True
    if role not in resource.get('audience',[]): return False
    if resource.get('scenario'):
        station=ident.get('station',0)
        scenario = None
        if role=='participant':
            original = event_config.original_scenario(s, ident['pair'], read_only=True)
            scenario = original['id'] if original else None
        elif role=='judge':
            # Downloads/help are read-only: do not initialise configuration or
            # upgrade a legacy Final while checking document authority.
            final = s.kv_get('final', {})
            pairs = final.get('pairs', [])
            engaged = final.get('status') in s.final_event.ENGAGED or final.get('active')
            if engaged and pairs:
                pair = pairs[final.get('current', 0)]
            elif (s.kv_get('calib', {}) or {}).get('active'):
                pair = None
            else:
                pair = event_config.pair_for(s, s.clock_public(station)['round'], station, read_only=True)
            original = event_config.original_scenario(s, pair, read_only=True)
            snapshot = final.get('teams', {}).get(str(pair), {}).get('case') if engaged else None
            scenario = (snapshot or original or s.scenario_for(station))['id']
        if role in ('judge','participant') and resource['scenario'] != scenario: return False
    return True


def usage_help(s,ident,context=None):
    """Select help from the authenticated role and reuse document authority."""
    data=json.loads((Path(s.HERE)/'usage-guides.json').read_text(encoding='utf-8'))
    role=ident['role']
    context=context or role
    if context=='desk' and ident.get('dashboard') in ('organizer','scoring') and ident['dashboard'] in data['roles']:context=ident['dashboard']
    guide=data['roles'][context]
    resources={r['id']:r for r in guide_data(s).get('resources',[])}
    downloads=[]
    for item in guide.get('downloads',[]):
        resource=resources.get(item.get('id'))
        if resource and resource_allowed(s,resource,ident):
            downloads.append({'id':resource['id'],'label':item['label'],'url':'/api/resources/'+resource['id']})
    return {'role':role,'context':context,'version':data['version'],
            **{key:guide[key] for key in ('title','summary','steps','sections')},'downloads':downloads}


def handle_get(s,h,path,q):
    if path not in ('/api/access/me','/api/help','/api/event','/api/participant/state','/api/admin/credentials','/api/admin/activity') and not path.startswith('/api/resources/'):
        return False
    ident=session(s,h.headers)
    # Local organiser tokens retain admin management for rehearsals.
    if not ident and not s.hosted() and s.who(h.headers):
        role,station,label=s.who(h.headers)
        ident={'role':'admin' if role=='desk' else role,'station':station,'label':label,'pair':0,'member':0}
    if path=='/api/access/me':
        h.send(200,session_info(ident)); return True
    if not ident:
        h.send(401,{'error':'Sign in to continue.'}); return True
    if path=='/api/participant/state':
        if ident['role']!='participant':
            h.send(403,{'error':'Participant access is required.'}); return True
        with s.LOCK:
            participant=participant_assignment(s,ident)
            if not participant:
                h.send(409,{'error':'Your assignment is unavailable. Ask the Event Desk to check your access.'}); return True
            # The session is the only authority for team/member selection.
            result=participant_companion.state(s,participant)
            import event_operations
            result['message_scope'] = event_operations.subject(s, ident)[0]
            import participant_workspace
            result['workspace'] = participant_workspace.participant_data(s, s.roster_row(participant['pair']))
        h.send(200,result); return True
    if path=='/api/help':
        context=q.get('context',[ident['role']])[0]
        if context!=ident['role'] and not (ident['role']=='admin' and context=='desk'):
            h.send(403,{'error':'This guide is not included in your access.'}); return True
        try:
            guide=usage_help(s,ident,context)
        except (OSError,ValueError,KeyError,TypeError):
            h.send(503,{'error':'The guide is temporarily unavailable. Try again.'})
            return True
        h.send(200,guide); return True
    if path.startswith('/api/admin/') and ident['role']!='admin':
        h.send(403,{'error':'Admin access is required.'}); return True
    if path=='/api/admin/credentials':
        seed_guest(s)
        with s.LOCK:
            creds=[dict(r) for r in auth_db(s).execute('SELECT '+CREDENTIAL_FIELDS+" FROM access_credentials c WHERE (dataset IN ('global',?) OR EXISTS (SELECT 1 FROM account_grants g WHERE g.account_id=c.account_id AND g.dataset IN ('global',?))) ORDER BY role,pair,member,label",(active_dataset(s),active_dataset(s)))]
            roster=event_config.list_roster(s)
            creds=[credential_view(s,c) for c in creds]
        h.send(200,{'credentials':creds,'participants':roster}); return True
    if path=='/api/admin/activity':
        try:
            limit=max(1,min(200,int(q.get('limit',['100'])[0])))
            before=int(q.get('before',['0'])[0])
        except ValueError:
            h.send(400,{'error':'Invalid page.'}); return True
        role=q.get('role',[''])[0]
        where=['dataset=?']; args=[active_dataset(s)]
        if before: where.append('id<?'); args.append(before)
        if role: where.append('role=?'); args.append(role)
        with s.LOCK,auth_db(s):
            prune(s)
            stats=auth_db(s).execute("SELECT count(DISTINCT visitor_id) visitors, sum(CASE WHEN action='view' THEN 1 ELSE 0 END) views, sum(CASE WHEN action='signin' THEN 1 ELSE 0 END) signins, sum(CASE WHEN action NOT IN ('view','signin','signin-failed') THEN 1 ELSE 0 END) actions FROM visitor_activity WHERE dataset=?",(active_dataset(s),)).fetchone()
            rows=[dict(r) for r in auth_db(s).execute('SELECT * FROM visitor_activity WHERE '+' AND '.join(where)+' ORDER BY id DESC LIMIT ?',(*args,limit))]
        h.send(200,{'summary':{k:stats[k] or 0 for k in stats.keys()},'events':rows,'next_before':rows[-1]['id'] if len(rows)==limit else None,'retention_days':RETENTION_DAYS}); return True
    if ident['role']=='registration':
        h.send(403,{'error':'Use the Registration Desk for participant information.'}); return True
    guide=guide_data(s)
    resources=guide.pop('resources',[])
    if path.startswith('/api/resources/'):
        rid=path.rsplit('/',1)[-1]
        item=next((r for r in resources if r['id']==rid),None)
        if not item:
            h.send(404,{'error':'Document not found.'}); return True
        if not resource_allowed(s,item,ident):
            h.send(403,{'error':'This document is not included in your access.'}); return True
        folder=(Path(s.HERE)/'resources').resolve()
        target=(folder/item['file']).resolve()
        if target.parent!=folder or not target.is_file():
            h.send(404,{'error':'Document not found.'}); return True
        h.send(200,target.read_bytes(),'application/pdf',{'Content-Disposition':'inline; filename="'+target.name.replace('"','')+'"'}); return True
    role=ident['role']
    participant=participant_assignment(s,ident)
    if role=='guest':
        guide={k:v for k,v in guide.items() if k in ('title','subtitle','date','venue','intro','schedule')}
    elif role in ('participant','judge'):
        current = s.assignment(ident['station']) if role == 'judge' else None
        original = event_config.original_scenario(s, current.get('pair')) if current and current.get('pair') else None
        scenario=participant['scenario'] if participant else original['id'] if original else s.scenario_for(ident['station'])['id']
        guide['scenarios']=[x for x in guide.get('scenarios',[]) if x['id']==scenario]
    safe_resources=[{k:r[k] for k in ('id','title','version','scenario') if k in r}|{'url':'/api/resources/'+r['id']} for r in resources if resource_allowed(s,r,ident)]
    h.send(200,{'guide':guide,'participant':participant,'now':s.public_state(names=False),'resources':safe_resources,'role':role,
                'configuration':event_config.config(s)}); return True


def handle_post(s,h,path,b):
    if path not in ('/api/access/login','/api/access/logout','/api/admin/credentials','/api/activity'):
        return False
    ident=session(s,h.headers)
    if path=='/api/access/login':
        ip,_=client_location(s,h)
        if s.throttled(ip):
            h.send(429,{'error':'Too many tries. Wait a minute.'}); return True
        pw=b.get('password','')
        requested=b.get('role')
        if (requested is not None and (not isinstance(requested,str) or requested not in ROUTES)) or not isinstance(pw,str) or not 1<=len(pw)<=128:
            h.send(400,{'error':'Enter your password.'}); return True
        seed_guest(s)
        with s.LOCK,auth_db(s):
            c=auth_db(s).execute('SELECT * FROM access_credentials WHERE lookup=? AND active=1',(lookup(s,pw),)).fetchone()
            role=cid=label=None; station=0
            if c and bool(event_roles.grants(s,c['account_id'])) and (requested is None or c['role']==requested or c['role']=='admin') and s.match_secret(password_hash(pw,c['password_hash'].split(':',1)[0]),c['password_hash']):
                role,cid,label=c['role'],c['id'],c['label']
                station=c['station']
                if role:
                    box = c['password_box'] or seal_password(s, pw, cid, c['revision'], c['dataset'])
                    auth_db(s).execute('UPDATE access_credentials SET last_used=?,password_box=? WHERE id=?',(time.time(),box,cid))
            elif s.match_secret(pw,s.CFG['password']):
                role,cid,label='admin','owner-recovery','Owner'
            if role:
                # Signing in replaces this browser's previous session.
                old=s.cookie_of(h.headers,'pp_access')
                if old:
                    auth_db(s).execute('DELETE FROM role_sessions WHERE digest=?',(s.credential_digest(old),))
                    auth_db(s).execute('UPDATE access_profiles SET revoked=1 WHERE parent_digest=?',(s.credential_digest(old),))
                token,ttl=mint_session(s,role,cid,label,station)
        if not role:
            s.throttled(ip,True)
            h.send(403,{'error':'That password did not match. Check it and try again.'}); return True
        h._activity_identity={'role':role,'credential_id':cid,'label':label}
        signed=root_session(s,{'Cookie':'pp_access='+token})
        result=session_info(signed)|{'ok':True}
        if requested is None:
            result['next']='/home'
        if role=='admin' and requested is not None and requested!='admin':
            result['next']='/registration' if requested=='registration' else '/admin?view='+requested+'#profiles'
        h._response_access_context=result['context']
        h.send(200,result,extra={'Set-Cookie':access_cookie(s,token,ttl)}); return True
    if not ident and not s.hosted() and s.who(h.headers):
        r,station,label=s.who(h.headers)
        ident={'role':'admin' if r=='desk' else r,'station':station,'label':label}
    if path=='/api/access/logout':
        if ident: h._activity_identity=ident
        token=s.cookie_of(h.headers,'pp_access')
        with s.LOCK,auth_db(s):
            if token:
                auth_db(s).execute('DELETE FROM role_sessions WHERE digest=?',(s.credential_digest(token),))
                auth_db(s).execute('UPDATE access_profiles SET revoked=1 WHERE parent_digest=?',(s.credential_digest(token),))
        h._response_access_context=''
        h.send(200,{'ok':True},extra={'Set-Cookie':access_cookie(s,'',0)}); return True
    if not ident:
        h.send(401,{'error':'Sign in to continue.'}); return True
    if path=='/api/activity':
        page=clean_path(str(b.get('page','')))
        if b.get('action')!='navigation' or page not in PAGES:
            h.send(400,{'error':'Invalid activity.'}); return True
        section=b.get('section','')
        sections={'overview','people','credentials','roster','access','content','reports','activity','system','run','scores','display','final','calib','notifications','workspace'}
        h._activity_action='navigation'+(':'+section if isinstance(section,str) and section in sections else ''); h._activity_path=page
        h.send(200,{'ok':True}); return True
    if ident['role']!='admin' or ident.get('impersonation'):
        h.send(403,{'error':'Admin access is required.'}); return True
    try:
        action=b.get('action')
        if action=='reveal':
            if not ident.get('digest') or not s.hosted():
                h.send(403,{'error':'Sign in to the hosted app as Admin to show a password.'}); return True
            with s.LOCK, auth_db(s):
                c = auth_db(s).execute("SELECT * FROM access_credentials c WHERE id=? AND active=1 AND (dataset IN ('global',?) OR EXISTS (SELECT 1 FROM account_grants g WHERE g.account_id=c.account_id AND g.dataset IN ('global',?)))",
                                      (str(b.get('id','')), active_dataset(s), active_dataset(s))).fetchone()
                if not c:
                    h.send(404,{'error':'Active password record not found.'}); return True
                try:
                    password = reveal_password(s, c)
                except ValueError as error:
                    h.send(409,{'error':str(error)}); return True
                result = s.event_mail.decorate_issuance(s, ident, {
                    'credential': credential_view(s, {field: c[field] for field in CREDENTIAL_FIELDS.split(',')}),
                    'password': password,
                })
                s.audit('admin', 'credential-reveal', c['id'])
            h._activity_action='credentials-reveal'
            h.send(200,result); return True
        with s.LOCK,auth_db(s):
            if s.hosted():
                ident = session(s, h.headers)
                if not ident or ident['role'] != 'admin' or ident.get('impersonation'):
                    h.send(401, {'error':'Your Admin session has ended.'}); return True
            if active_dataset(s) == 'rehearsal':
                global_change = action == 'create' and b.get('role') in ('admin','desk','organizer','scoring','registration','guest')
                if action in ('reset','revoke'):
                    with s.LOCK:
                        target = auth_db(s).execute("SELECT 1 FROM access_credentials c JOIN account_grants g ON g.account_id=c.account_id WHERE c.id=? AND g.dataset!='rehearsal'",(str(b.get('id','')),)).fetchone()
                    global_change = target is not None
                if global_change:
                    h.send(409,{'error':'Switch to Live to create or change staff and guest access. Rehearsal only changes practice participant passwords.'})
                    return True
            if action=='create-staff':
                result=create_staff(s,b,ident)
            elif action=='create':
                result=create_credential(s,b.get('role'),b.get('label'),b.get('pair',0),b.get('member',0),b.get('password'),station=b.get('station',0))
            elif action in ('reset','revoke'):
                c=auth_db(s).execute('SELECT * FROM access_credentials WHERE id=?',(str(b.get('id','')),)).fetchone()
                if not c: raise ValueError('Access record not found.')
                account=auth_db(s).execute('SELECT * FROM person_accounts WHERE id=? AND active=1',(c['account_id'],)).fetchone()
                available=event_roles.grants(s,c['account_id'])
                if not account or not available: raise ValueError('This account has no access in the current event. Assign a role first.')
                event_roles.revoke_sessions(auth_db(s),c['account_id'])
                if action=='revoke':
                    event_roles.protect_revoke(s,c)
                    auth_db(s).execute('UPDATE access_credentials SET active=0,password_box=NULL WHERE id=?',(c['id'],))
                    result={'ok':True}
                else:
                    for grant in auth_db(s).execute('SELECT * FROM account_grants WHERE account_id=?',(c['account_id'],)).fetchall():
                        event_roles.validate_unique(s,grant,c['account_id'])
                    # Rotate the retained identity, not its obsolete original assignment.
                    password=b.get('password') if b.get('password') is not None else secrets.token_urlsafe(12)
                    minimum=8 if all(g['role']=='guest' for g in available) else 10
                    if not isinstance(password,str) or not minimum<=len(password)<=128:
                        raise ValueError('Use a password of 8–128 characters for guests or 10–128 for individual access.')
                    if s.match_secret(password,s.CFG['password']):
                        raise ValueError('Choose a different password from the owner recovery password.')
                    revision=c['revision']+1
                    auth_db(s).execute('UPDATE access_credentials SET lookup=?,password_hash=?,password_box=?,revision=?,active=1 WHERE id=?',
                        (lookup(s,password),password_hash(password),seal_password(s,password,c['id'],revision,c['dataset']),revision,c['id']))
                    auth_db(s).execute('UPDATE person_accounts SET revision=revision+1 WHERE id=?',(c['account_id'],))
                    row=dict(auth_db(s).execute('SELECT '+CREDENTIAL_FIELDS+' FROM access_credentials WHERE id=?',(c['id'],)).fetchone())
                    result={'credential':row,'password':password}
            elif action=='bulk-participants':
                issued=[]; skipped=0
                rows=event_config.list_roster(s)
                for row in rows:
                    for person in row['members']:
                        member, label = person['id'], person['name']
                        if not label.strip(): continue
                        exists=auth_db(s).execute("SELECT 1 FROM account_grants g JOIN access_credentials c ON c.account_id=g.account_id AND c.active=1 WHERE g.role='participant' AND g.pair=? AND g.member=? AND g.dataset=?",(row['pair'],member,active_dataset(s))).fetchone()
                        if exists: skipped+=1; continue
                        item=create_credential(s,'participant',label,row['pair'],member)
                        issued.append(item['credential']|{'password':item['password']})
                result={'issued':issued,'skipped':skipped}
            else: raise ValueError('Choose a valid password management action.')
            if result.get('credential'):
                result['credential'] = credential_view(s,result['credential'])
            if action in ('create','create-staff','reset','bulk-participants'):
                # The mail helper only grants optional sharing; it sends nothing.
                result=s.event_mail.decorate_issuance(s,ident,result)
        h._activity_action='credentials-'+action
        h.send(200,result)
    except (ValueError, *database.integrity_errors()) as e:
        message=str(e) if isinstance(e,ValueError) else 'That password or participant already has an access record. Use reset for an existing record.'
        h.send(400,{'error':message})
    return True
