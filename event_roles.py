"""Person grants and per-request dashboard authority. No mutable active-role cookie."""
import json
import secrets
import sqlite3
from contextlib import closing
import time

ROLES = {
 'guest': ('Event information','/guest'), 'participant': ('My Event','/participant'),
 'judge': ('Judging','/judge'), 'registration': ('Registration Desk','/registration'),
 'organizer': ('Event Operations','/organizer'), 'scoring': ('Judging Operations','/scoring'),
 'admin': ('Event Administration','/admin')}
STAFF = {'admin','organizer','scoring','registration','judge'}
ORDER = ('admin','organizer','scoring','registration','judge','participant','guest')

def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS person_accounts (
      id TEXT PRIMARY KEY, label TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
      active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS account_grants (
      account_id TEXT NOT NULL, role TEXT NOT NULL, dataset TEXT NOT NULL,
      station INTEGER NOT NULL DEFAULT 0, pair INTEGER NOT NULL DEFAULT 0,
      member INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account_id,role,dataset));
    ''')
    for table, name, spec in [('access_credentials','account_id',"TEXT NOT NULL DEFAULT ''"),
        ('role_sessions','assurance',"TEXT NOT NULL DEFAULT 'password'"),
        ('role_sessions','account_revision','INTEGER NOT NULL DEFAULT 0')]:
        if name not in {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}:
            db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+spec)
    if 'subject' not in {r['name'] for r in db.execute('PRAGMA table_info(account_grants)')}:
        db.execute("ALTER TABLE account_grants ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
    for c in db.execute('SELECT * FROM access_credentials').fetchall():
        ensure_account(db,c,legacy=True)


def ensure_account(db,c,legacy=False):
    aid=c['account_id'] if 'account_id' in c.keys() and c['account_id'] else c['id']
    if db.execute('SELECT 1 FROM person_accounts WHERE id=?',(aid,)).fetchone():
        if not c['account_id']:db.execute('UPDATE access_credentials SET account_id=? WHERE id=?',(aid,c['id']))
        return aid
    db.execute('INSERT INTO person_accounts(id,label,created,active) VALUES(?,?,?,?)',
               (aid,c['label'] or 'Legacy account',c['created'] or time.time(),c['active']))
    roles={'desk':['organizer','scoring']}.get(c['role'],[c['role']])
    if legacy and c['role']=='admin':roles=['admin','organizer','scoring','registration']
    for role in roles:
        db.execute('INSERT INTO account_grants VALUES(?,?,?,?,?,?,?)',
                   (aid,role,c['dataset'],c['station'],c['pair'],c['member'],(c['label'] or '') if role=='participant' else ''))
    db.execute('UPDATE access_credentials SET account_id=? WHERE id=?',(aid,c['id']))
    return aid


def grants(s,aid,assurance='password'):
    rows=[dict(r) for r in s.event_access.auth_db(s).execute(
        "SELECT * FROM account_grants WHERE account_id=? AND dataset IN ('global',?)",
        (aid,s.event_access.active_dataset(s)))]
    if assurance=='participant_qr': rows=[r for r in rows if r['role']=='participant']
    return sorted(rows,key=lambda r:ORDER.index(r['role']))


def enrich(s,out,c):
    db=s.event_access.auth_db(s)
    if c is None:
        out.update(account_id='owner-recovery',account_label='Owner',account_revision=1,
          grants=[dict(role=r,dataset='global',station=0,pair=0,member=0) for r in ('admin','organizer','scoring','registration')])
        return out
    aid=ensure_account(db,c)
    account=db.execute('SELECT * FROM person_accounts WHERE id=? AND active=1',(aid,)).fetchone()
    if not account or out.get('account_revision',0) not in (0,account['revision']): return None
    out.update(account_id=aid,account_label=account['label'],account_revision=account['revision'],
               grants=grants(s,aid,out.get('assurance','password')))
    return out


def dashboards(ident):
    return [dict(role=g['role'],label=ROLES[g['role']][0],url=(f"/judge/{g['station']}" if g['role']=='judge' else ROLES[g['role']][1])) for g in ident.get('grants',[])]


def select(s,headers,parent):
    if not parent or parent.get('impersonation'): return parent
    selected=headers.get('X-Dashboard')
    if not selected:
        # Old clients remain scoped to their original credential role, never the union.
        selected=parent['role']
    if headers.get('X-Dashboard'):
        parent=dict(parent,context=s.event_profiles.context(s,parent['context']+':dashboard:'+selected))
    if selected=='home':
        return dict(parent,role='home',dashboard='home',station=0,pair=0,member=0)
    available=parent.get('grants',[])
    if selected=='desk' and headers.get('X-Dashboard'):return None
    if selected=='desk':
        if {'organizer','scoring'} <= {g['role'] for g in available}:
            return dict(parent,role='desk',dashboard='desk',station=0,pair=0,member=0)
        selected='organizer' if any(g['role']=='organizer' for g in available) else 'scoring'
    g=next((g for g in available if g['role']==selected),None)
    if not g: return None
    if selected=='judge' and not 1<=g['station']<=s.CFG['stations']:return None
    if selected=='participant':
        row=s.event_config.roster_row(s,g['pair'])
        if not row or not 1<=g['member']<=len(row['members']) or row['members'][g['member']-1]['name']!=g.get('subject'):return None
    out=dict(parent,role='desk' if selected in ('organizer','scoring') else selected,dashboard=selected,
             station=g['station'],pair=g['pair'],member=g['member'],dataset=g['dataset'])
    if selected=='judge':out['draft_key']=s.event_profiles.draft_key(s,out)
    return out


def guard(s,h,path,body=None):
    if not s.hosted() or not path.startswith('/api/'):return False
    if path in ('/api/access/login','/api/access/logout', '/api/access/registration-link'):return False
    parent=s.event_access.root_session(s,h.headers)
    ident=s.event_access.session(s,h.headers)
    if h.headers.get('X-Dashboard') and parent and not ident:
        h.send(403,{'code':'dashboard_forbidden','error':'This dashboard is not assigned to you.'});return True
    if not ident:return False # Domain handlers retain their unauthenticated checks.
    role=ident.get('dashboard',ident['role'])
    if role=='admin':
        operations={'/api/desk/clock':'scoring','/api/desk/mode':'scoring','/api/desk/sheet':'scoring','/api/desk/calib':'scoring','/api/desk/reveal':'scoring','/api/desk/final':'scoring','/api/desk/public':'scoring','/api/desk/audience':'organizer','/api/desk/program':'organizer','/api/desk/quiz':'organizer','/api/registration':'registration'}
        if h.command=='POST' and path in operations:
            if h.headers.get('X-Dashboard') or operations[path] not in {g['role'] for g in ident.get('grants',[])}:
                h.send(403,{'error':'Open the assigned operational dashboard for this action.'});return True
        if path=='/api/desk/participant-workspace' and h.command=='POST':
            required='scoring' if (body or {}).get('action')=='release' else 'organizer'
            if h.headers.get('X-Dashboard') or (body is not None and required not in {g['role'] for g in ident.get('grants',[])}):
                h.send(403,{'error':'Open Event Operations or Judging Operations for this action.'});return True
        return False
    if role=='desk' or ident.get('impersonation') and role not in ('organizer','scoring'):return False
    if role=='home':
        if path in ('/api/access/me','/api/activity','/api/info','/api/public','/api/board','/api/access/logout'):return False
        h.send(403,{'error':'Open an assigned dashboard first.'});return True
    if ident.get('impersonation') and path in ('/api/access/impersonate','/api/access/impersonate/end'):return False
    common={'/api/access/me','/api/activity','/api/info','/api/public','/api/board','/api/help','/api/event','/api/access/logout'}
    if path in common or path.startswith('/api/resources/'):return False
    if role=='home':
        h.send(403,{'error':'Open an assigned dashboard first.'});return True
    if role not in ('organizer','scoring'):return False
    gets={'/api/content','/api/desk/state','/api/event-operations'}
    posts={'/api/event-operations'}
    if role=='organizer':
        gets|={'/api/desk/audience','/api/desk/audience/responses','/api/desk/quiz','/api/desk/quiz/questions.csv','/api/desk/participant-workspace'}
        posts|={'/api/desk/audience','/api/desk/program','/api/desk/quiz','/api/desk/participant-workspace'}
    else:
        gets|={'/api/desk/scores','/api/desk/sheets','/api/desk/calib','/api/desk/export.csv','/api/desk/participant-workspace'}
        posts|={'/api/desk/public','/api/desk/clock','/api/desk/mode','/api/desk/reveal','/api/desk/final','/api/desk/calib','/api/desk/sheet','/api/desk/ai-summary','/api/desk/participant-workspace'}
    allowed=path in (posts if h.command=='POST' else gets) or path=='/api/assistant' or path.startswith('/api/assistant/')
    if path=='/api/desk/participant-workspace' and body is not None:
        allowed=body.get('action') in ({'post_news','edit_news','expire_news','nominations'} if role=='organizer' else {'release'})
    if not allowed:h.send(403,{'error':'This action is not available in this dashboard.'});return True
    return False


def desk_projection(s,ident,data):
    role=(ident or {}).get('dashboard')
    if role!='organizer':return data
    safe={k:data[k] for k in ('now','program','room','agenda','versions','public','configuration','config','rounds','judge_count','pairs','rehearsal','port','anonymous_view_key','clock','mode') if k in data}
    safe['stations']=[{k:r[k] for k in ('station','submitted','seen','pair','clock') if k in r} for r in data['stations']]
    safe['final']=s.public_state(names=False).get('final',{})
    return safe


def validated(s,body):
    values=body.get('grants')
    if not isinstance(values,list) or len(values)>len(ROLES):raise ValueError('Choose valid role assignments.')
    out=[];seen=set()
    for value in values:
        if not isinstance(value,dict) or value.get('role') not in ROLES:raise ValueError('Choose a valid role.')
        role=value['role']
        if role in seen:raise ValueError('Assign each role once per event dataset.')
        seen.add(role)
        dataset=s.event_access.active_dataset(s) if role in ('participant','judge') else 'global'
        station=value.get('station',0);pair=value.get('pair',0);member=value.get('member',0)
        if any(type(v) is not int for v in (station,pair,member)):raise ValueError('Use valid assignment identifiers.')
        if role=='judge':
            if not 1<=station<=s.CFG['stations'] or pair or member:raise ValueError('Choose the judging station.')
        elif role=='participant':
            row=s.event_config.roster_row(s,pair)
            if not row or not 1<=member<=len(row['members']) or not row['members'][member-1]['name'].strip() or station:raise ValueError('Choose a named participant.')
        elif station or pair or member:raise ValueError('This role does not use an assignment.')
        out.append(dict(role=role,dataset=dataset,station=station,pair=pair,member=member,subject=row['members'][member-1]['name'] if role=='participant' else ''))
    if 'guest' in seen and len(seen)>1:raise ValueError('Shared Guest access cannot hold other roles.')
    return out


def revoke_sessions(db,aid):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='registration_links'").fetchone():
        db.execute('DELETE FROM registration_links WHERE credential_id IN (SELECT id FROM access_credentials WHERE account_id=?)',(aid,))
    db.execute('UPDATE access_profiles SET revoked=1 WHERE parent_digest IN (SELECT digest FROM role_sessions WHERE credential_id IN (SELECT id FROM access_credentials WHERE account_id=?))',(aid,))
    db.execute('DELETE FROM role_sessions WHERE credential_id IN (SELECT id FROM access_credentials WHERE account_id=?)',(aid,))



def account_type(s, aid):
    db = s.event_access.auth_db(s)
    assigned = {row[0] for row in db.execute('SELECT role FROM account_grants WHERE account_id=?', (aid,))}
    original = {row[0] for row in db.execute('SELECT role FROM access_credentials WHERE account_id=?', (aid,))}
    if assigned & STAFF or original & (STAFF | {'desk'}):
        return 'staff'
    return 'participant' if 'participant' in assigned | original else 'guest'


def protect_quiz_link(s, accounts):
    """Preserve quiz/Audience identity in both existing workspaces."""
    from audience_questions import KEY as AUDIENCE_KEY, answer_key
    db = s.event_access.auth_db(s)
    credentials = {row[0] for row in db.execute('SELECT id FROM access_credentials WHERE account_id IN (?,?)', accounts)}
    def check(conn):
        states = []
        for key in ('event_quiz_v1', 'event_quiz_reset_archive_v1'):
            row = conn.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
            if not row:
                continue
            value = json.loads(row[0])
            if key == 'event_quiz_v1':
                states.append(value)
            else:
                if not isinstance(value, list) or any(not isinstance(item, dict) or not isinstance(item.get('state'), dict) for item in value):
                    raise ValueError('Quiz history could not be checked. Keep the existing accounts and try again after recovery.')
                states.extend(item['state'] for item in value)
        for state in states:
            if not isinstance(state, dict):
                raise ValueError('Quiz history could not be checked. Keep the existing accounts and try again after recovery.')
            touched = set(state.get('members', {})) | set(state.get('answers', {}))
            for viewers in state.get('viewed', {}).values():
                touched.update(viewers)
            if credentials & touched:
                raise ValueError('These accounts have quiz activity, including saved reset history. Keep the participant’s existing account and add staff roles to it instead; linking would lose quiz progress.')
        row = conn.execute('SELECT v FROM kv WHERE k=?',(AUDIENCE_KEY,)).fetchone()
        if row:
            packs = json.loads(row[0])['packs']
            keys = [answer_key(p['id'],account) for p in packs for account in accounts]
            if keys and conn.execute('SELECT 1 FROM kv WHERE k IN ('+','.join('?' for _ in keys)+') LIMIT 1',
                                     keys).fetchone():
                raise ValueError('These accounts have Audience responses. Keep the participant’s existing account and add staff roles to it instead.')
    try:
        check(db)
        if s.WORKSPACE:
            if s.WORKSPACE.rehearsal is not None:
                check(s.WORKSPACE.rehearsal)
            else:
                path = s.WORKSPACE.root / 'rehearsal' / 'judging.db'
                if path.exists():
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
                        check(conn)
    except (sqlite3.Error, json.JSONDecodeError, TypeError, AttributeError, KeyError):
        raise ValueError('Quiz history could not be checked. Keep the existing accounts and try again after recovery.') from None


def handle(s,h,path,body=None):
    if path!='/api/admin/accounts':return False
    ident=s.event_access.session(s,h.headers)
    if not ident or ident['role']!='admin' or ident.get('impersonation'):
        h.send(403 if ident else 401,{'error':'Admin access is required.'});return True
    db=s.event_access.auth_db(s)
    try:
        with s.LOCK,db:
            ident=s.event_access.session(s,h.headers)
            if not ident or ident['role']!='admin' or ident.get('impersonation'):
                h.send(401,{'error':'Your Admin session has ended.'});return True
            if body is None:
                rows=[]
                for a in db.execute('SELECT * FROM person_accounts WHERE active=1 ORDER BY label').fetchall():
                    rows.append(dict(a,account_type=account_type(s,a['id']),grants=grants(s,a['id']),credentials=[dict(c) for c in db.execute('SELECT id,label,role,active,last_used FROM access_credentials WHERE account_id=?',(a['id'],))]))
                h.send(200,{'accounts':rows,'roles':[dict(id=r,label=ROLES[r][0]) for r in ORDER]});return True
            aid=body.get('id');a=db.execute('SELECT * FROM person_accounts WHERE id=? AND active=1',(aid,)).fetchone()
            if not a:raise ValueError('Account not found.')
            if body.get('revision')!=a['revision']:
                h.send(409,{'error':'Access changed. Refresh this person before saving.'});return True
            reason=body.get('reason','')
            if not isinstance(reason,str) or not 3<=len(reason.strip())<=300:raise ValueError('Enter a short reason for this access change.')
            old=grants(s,aid)
            shared_confirmation_required=False
            if body.get('action')=='roles':
                new=validated(s,body)
                for g in new:validate_unique(s,g,aid)
                if s.event_access.active_dataset(s)=='rehearsal':
                    before=[{k:g[k] for k in ('role','dataset','station','pair','member','subject')} for g in old if g['dataset']=='global']
                    after=[g for g in new if g['dataset']=='global']
                    shared_confirmation_required=sorted(before,key=lambda g:g['role'])!=sorted(after,key=lambda g:g['role'])
                    if shared_confirmation_required and body.get('confirm_shared_roles') is not True:
                        raise ValueError('Confirm that shared staff-role changes apply in Live and Rehearsal.')
                if any(g['role']=='guest' for g in old) and new!=old:raise ValueError('Shared Guest access cannot be reassigned.')
                if aid==ident.get('account_id') and not any(g['role']=='admin' for g in new):raise ValueError('Keep your own Admin access; another Admin must remove it.')
                if any(g['role']=='admin' for g in old) and not any(g['role']=='admin' for g in new):
                    count=db.execute("SELECT count(DISTINCT a.id) FROM person_accounts a JOIN account_grants g ON g.account_id=a.id JOIN access_credentials c ON c.account_id=a.id WHERE a.active=1 AND c.active=1 AND g.role='admin' AND a.id<>?",(aid,)).fetchone()[0]
                    if not count:raise ValueError('Keep at least one active Admin account.')
                db.execute("DELETE FROM account_grants WHERE account_id=? AND dataset IN ('global',?)",(aid,s.event_access.active_dataset(s)))
                for g in new:db.execute('INSERT INTO account_grants VALUES(?,?,?,?,?,?,?)',(aid,g['role'],g['dataset'],g['station'],g['pair'],g['member'],g['subject']))
            elif body.get('action')=='link':
                if s.event_access.active_dataset(s)=='rehearsal':raise ValueError('Switch to Live to link accounts; this retires a shared password.')
                if not db.execute('SELECT 1 FROM access_credentials WHERE account_id=? AND active=1',(aid,)).fetchone():raise ValueError('The retained account must have an active password.')
                source=body.get('source_id')
                src=db.execute('SELECT * FROM person_accounts WHERE id=? AND active=1',(source,)).fetchone()
                if not src or source==aid:raise ValueError('Choose another existing account to consolidate.')
                if body.get('source_revision')!=src['revision']:raise ValueError('Refresh the source account before linking.')
                if any(g['role']=='guest' for g in old+grants(s,source)):raise ValueError('Guest access cannot be linked to a person.')
                if body.get('confirm')!='LINK AND RETIRE SOURCE PASSWORD':raise ValueError('Confirm that the selected source password will be retired.')
                if source==ident.get('account_id'):raise ValueError('Retain the account you are currently signed in with.')
                if db.execute('SELECT 1 FROM account_grants a JOIN account_grants b ON a.role=b.role AND a.dataset=b.dataset WHERE a.account_id=? AND b.account_id=?',(aid,source)).fetchone():raise ValueError('Resolve overlapping role assignments before linking.')
                protect_quiz_link(s, (aid, source))
                revoke_sessions(db,source)
                db.execute('UPDATE account_grants SET account_id=? WHERE account_id=?',(aid,source))
                db.execute('UPDATE access_credentials SET active=0,password_box=NULL WHERE account_id=?',(source,))
                db.execute('UPDATE person_accounts SET active=0,revision=revision+1 WHERE id=?',(source,))
            else:raise ValueError('Choose an account action.')
            db.execute('UPDATE person_accounts SET revision=revision+1 WHERE id=?',(aid,))
            revoke_sessions(db,aid)
            if shared_confirmation_required:
                # Persist only shared role changes in shared auth storage. Practice roster
                # subjects and assignments stay in the disposable rehearsal audit below.
                detail=json.dumps({'dataset':'rehearsal','shared_roles_confirmed':True,
                    'account_id':aid,'before':[g['role'] for g in old if g['dataset']=='global'],
                    'after':[g['role'] for g in new if g['dataset']=='global'],'reason':reason})
                db.execute('INSERT INTO audit(ts,actor,action,detail,actor_credential,actor_label,effective_credential,effective_role,profile_id,impersonation_mode) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (time.time(),'admin','account-shared-roles',detail,ident.get('credential_id',''),
                     ident.get('label',''),ident.get('credential_id',''),'admin','',''))
            s.audit('admin','account-'+body['action'],json.dumps({'dataset':s.event_access.active_dataset(s),
                'shared_roles_confirmed':shared_confirmation_required,'account_id':aid,'before':old,
                'after':grants(s,aid),'reason':reason,'source_id':body.get('source_id')}))
            h.send(200,{'ok':True,'revision':a['revision']+1,'reauthenticate':aid==ident.get('account_id')})
    except (ValueError,TypeError) as exc:h.send(400,{'error':str(exc)})
    return True


def reconcile_participants(s,incoming):
    db=s.event_access.auth_db(s)
    for g in db.execute("SELECT * FROM account_grants WHERE role='participant' AND dataset=?",(s.event_access.active_dataset(s),)).fetchall():
        if incoming.get((g['pair'],g['member']))!=g['subject']:
            db.execute("DELETE FROM account_grants WHERE account_id=? AND role='participant' AND dataset=?",(g['account_id'],g['dataset']))
            db.execute('UPDATE person_accounts SET revision=revision+1 WHERE id=?',(g['account_id'],))
            revoke_sessions(db,g['account_id'])
            if not db.execute('SELECT 1 FROM account_grants WHERE account_id=?',(g['account_id'],)).fetchone():
                db.execute('UPDATE access_credentials SET active=0,password_box=NULL WHERE account_id=?',(g['account_id'],))


def protect_revoke(s,credential):
    db=s.event_access.auth_db(s)
    aid=credential['account_id']
    if db.execute("SELECT 1 FROM account_grants WHERE account_id=? AND role='admin'",(aid,)).fetchone():
        other=db.execute("SELECT 1 FROM access_credentials c JOIN person_accounts a ON a.id=c.account_id JOIN account_grants g ON g.account_id=a.id WHERE a.active=1 AND c.active=1 AND g.role='admin' AND c.id<>?",(credential['id'],)).fetchone()
        if not other:raise ValueError('Keep at least one active Admin account before revoking this password.')


def validate_unique(s,g,aid=''):
    if g['role'] not in ('participant','judge'):return
    field='station' if g['role']=='judge' else 'pair'
    query="""SELECT 1 FROM account_grants g JOIN person_accounts a ON a.id=g.account_id AND a.active=1
      JOIN access_credentials c ON c.account_id=a.id AND c.active=1
      WHERE g.role=? AND g.dataset=? AND g.account_id<>? AND g."""+field+"=?"
    args=[g['role'],g['dataset'],aid,g[field]]
    if g['role']=='participant':query+=' AND g.member=?';args.append(g['member'])
    if s.event_access.auth_db(s).execute(query,args).fetchone():
        raise ValueError('This assignment already belongs to another active account. Reassign or link that account first.')
