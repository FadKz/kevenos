"""Hosted role boundaries, passwords, document scope and activity attribution."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import test_api as T
S = T.S


def request(method, path, body=None, cookie='', headers=None, auto_context=True):
    headers = dict(headers or {})
    effective_cookie = cookie or headers.get('Cookie', '')
    if auto_context and effective_cookie and method == 'POST' and 'X-Access-Context' not in headers and path not in ('/api/access/login', '/api/access/logout'):
        status, identity, _ = request('GET', '/api/access/me', cookie=effective_cookie, headers={k:v for k,v in headers.items() if k == 'X-Profile-Token'}, auto_context=False)
        if status == 200 and identity.get('context'): headers['X-Access-Context'] = identity['context']
    raw = json.dumps(body).encode() if body is not None else b''
    h = {'Host':'event.example.com','Content-Type':'application/json','Content-Length':str(len(raw))}
    if cookie: h['Cookie'] = cookie
    h.update(headers or {})
    data=(method+' '+path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in h.items())+'\r\n').encode()+raw
    sock=T.Sock(data)
    class Handler(S.Handler):
        def setup(self): self.rfile=sock.rf; self.wfile=sock.wf
        def finish(self): pass
    Handler(sock,('172.19.0.2',1000),T.Srv())
    head,_,data=sock.wf.getvalue().partition(b'\r\n\r\n')
    status=int(head.split()[1])
    hdrs={}
    for line in head.decode().split('\r\n')[1:]:
        if ': ' in line:
            key,value=line.split(': ',1); hdrs.setdefault(key.lower(),[]).append(value)
    if 'application/json' in hdrs.get('content-type',[''])[0]: data=json.loads(data)
    return status,data,hdrs


def role_for(password):
    if password == S.CFG['password']: return 'admin'
    if password == S.CFG.get('guest_password'): return 'guest'
    db = getattr(S, 'AUTH_DB', None) or S.DB
    row = db.execute('SELECT role FROM access_credentials WHERE lookup=?', (S.event_access.lookup(S, password),)).fetchone()
    return row['role'] if row else 'guest'


class EventAccess(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.previous=S.CFG,S.DB,S.DATA,S.CONTENT
        self.addCleanup(self.restore)
        S.DATA=self.tmp.name
        S.CFG=S.load_config(str(Path(S.DATA)/'config.json'))
        S.CFG['guest_password']='SyntheticGuest8!'
        S.DB=S.open_db(str(Path(S.DATA)/'db'))
        S.CONTENT=S.bundled_content(); S.FAILS.clear()
        env=patch.dict(os.environ,{'KEVENOS_HOSTED':'1','KEVENOS_GATEWAY_ENABLED':'0','KEVENOS_TRUST_CF_HEADERS':'1','KEVENOS_CF_LOCATION_HEADERS':'1'})
        env.start(); self.addCleanup(env.stop)
        self.admin=self.login(S.CFG['password'],S.CFG['desk_pin'])

    def restore(self):
        S.DB.close(); S.CFG,S.DB,S.DATA,S.CONTENT=self.previous

    def login(self,password,pin=None):
        c,r,h=request('POST','/api/access/login',{'role':role_for(password),'password':password})
        self.assertEqual(c,200,r)
        self.assertNotIn('token',r)
        cookies=h['set-cookie']; value=next(x for x in cookies if x.startswith('pp_access='))
        self.assertIn('HttpOnly',value); self.assertIn('Secure',value)
        return value.split(';')[0]

    def create(self,role,**fields):
        c,r,_=request('POST','/api/admin/credentials',{'action':'create','role':role,'label':'Example '+role,**fields},self.admin)
        self.assertEqual(c,200,r); return r

    def test_guest_cannot_manage_or_read_private_content(self):
        guest=self.login(S.CFG['guest_password'])
        c,r,_=request('GET','/api/access/me',cookie=guest)
        self.assertEqual(r['role'],'guest')
        for p in ('/api/admin/credentials','/api/admin/activity','/api/content','/api/desk/state'):
            self.assertEqual(request('GET',p,cookie=guest)[0],403,p)
        self.assertEqual(request('POST','/api/desk/reset',{'confirm':'RESET'},guest)[0],403)
        c,event,_=request('GET','/api/event',cookie=guest)
        self.assertEqual(c,200,event); self.assertIsNone(event['participant'])
        self.assertEqual(event['resources'],[])
        for key in ('scenarios','steps','reading','scoring'): self.assertNotIn(key,event['guide'])
        self.assertEqual(request('POST','/api/access/login',{'role':'guest','password':S.CFG['guest_password'].lower()})[0],403)

    def test_guest_schedule_follows_published_admin_content(self):
        guest=self.login(S.CFG['guest_password'])
        content=dict(S.CONTENT)
        content['event']=dict(content['event'],venue='Updated event venue')
        content['agenda']=[dict(a) for a in content['agenda']]
        content['agenda'][0]['title']='Updated welcome'
        self.assertEqual(request('POST','/api/desk/content',{'content':content,'note':'Synthetic schedule edit'},self.admin)[0],200)
        _,event,_=request('GET','/api/event',cookie=guest)
        self.assertEqual(event['guide']['venue'],'Updated event venue')
        self.assertEqual(event['guide']['schedule'][0]['title'],'Updated welcome')
        self.assertNotIn('mock',event['guide'])

    def test_desk_can_run_live_but_cannot_administer_credentials_or_content(self):
        issued=self.create('desk')
        desk=self.login(issued['password'])
        self.assertEqual(request('GET','/api/desk/state',cookie=desk)[0],200)
        self.assertEqual(request('POST','/api/desk/program',{'action':'announce','text':'Welcome'},desk)[0],200)
        for p in ('/api/admin/credentials','/api/admin/activity','/api/desk/roster','/api/desk/backup.json'):
            self.assertEqual(request('GET',p,cookie=desk)[0],403,p)
        self.assertEqual(request('POST','/api/desk/content',{'action':'bundled'},desk)[0],403)
        self.assertEqual(request('POST','/api/admin/credentials',{'action':'revoke','id':issued['credential']['id']},desk)[0],403)

    def test_password_reset_revokes_sessions_and_never_returns_stored_passwords(self):
        issued=self.create('desk'); desk=self.login(issued['password'])
        _,rows,_=request('GET','/api/admin/credentials',cookie=self.admin)
        self.assertNotIn(issued['password'],json.dumps(rows))
        _,reset,_=request('POST','/api/admin/credentials',{'action':'reset','id':issued['credential']['id']},self.admin)
        self.assertEqual(request('GET','/api/desk/state',cookie=desk)[0],401)
        self.assertEqual(request('POST','/api/access/login',{'role':issued['credential']['role'],'password':issued['password']})[0],403)
        self.login(reset['password'])
        blob=' '.join(str(tuple(r)) for r in S.DB.execute('SELECT * FROM access_credentials'))
        self.assertNotIn(issued['password'],blob); self.assertNotIn(reset['password'],blob)

    def test_participant_only_gets_own_assignment_without_packaged_event_files(self):
        request('POST','/api/desk/roster',{'rows':[{'pair':1,'p1':'Alex Example','p2':'Sam Example','partner':'Example team'},{'pair':3,'p1':'Other Person','p2':'Other Partner'}]},self.admin)
        issued=self.create('participant',pair=1,member=1)
        participant=self.login(issued['password'])
        c,r,_=request('GET','/api/event',cookie=participant)
        self.assertEqual(c,200,r); self.assertEqual(r['participant']['name'],'Alex Example')
        self.assertEqual(r['participant']['pair'],1)
        self.assertNotIn('Other Person',json.dumps(r))
        resources={x['id'] for x in r['resources']}
        self.assertEqual(resources,set())
        self.assertEqual(request('GET','/api/resources/participant-pack-s2',cookie=participant)[0],404)
        self.assertEqual(request('GET','/api/resources/judge-guide',cookie=participant)[0],404)
        self.assertEqual(request('GET','/api/resources/participant-guide',cookie=participant)[0],404)

    def test_tracking_uses_identity_ip_geo_without_secret_or_query(self):
        guest=self.login(S.CFG['guest_password'])
        hdr={'CF-Connecting-IP':'203.0.113.20','CF-IPCountry':'AE','CF-IPCity':'local region','CF-Region':'local region','CF-Ray':'synthetic-DXB'}
        request('GET','/guest?password=DO-NOT-STORE',cookie=guest,headers=hdr)
        c,r,_=request('GET','/api/admin/activity',cookie=self.admin)
        self.assertEqual(c,200,r)
        e=next(x for x in r['events'] if x['path']=='/guest')
        self.assertEqual((e['ip'],e['country'],e['city'],e['role']),('203.0.113.20','AE','local region','guest'))
        self.assertEqual(e['credential_id'],'guest')
        serialized=json.dumps(r)
        self.assertNotIn('DO-NOT-STORE',serialized); self.assertNotIn(S.CFG['guest_password'],serialized)

    def test_roster_replacement_revokes_the_previous_participant(self):
        rows=[{'pair':1,'p1':'Alex Example','p2':'Sam Example'}]
        request('POST','/api/desk/roster',{'rows':rows},self.admin)
        issued=self.create('participant',pair=1,member=1)
        participant=self.login(issued['password'])
        rows[0]['p1']='Different Person'
        self.assertEqual(request('POST','/api/desk/roster',{'rows':rows},self.admin)[0],200)
        self.assertEqual(request('GET','/api/event',cookie=participant)[0],401)
        self.assertEqual(request('POST','/api/access/login',{'role':issued['credential']['role'],'password':issued['password']})[0],403)

    def test_bulk_issues_unique_passwords_once_and_logout_revokes(self):
        request('POST','/api/desk/roster',{'rows':[{'pair':1,'p1':'Alex Example','p2':'Sam Example'}]},self.admin)
        _,r,_=request('POST','/api/admin/credentials',{'action':'bulk-participants'},self.admin)
        self.assertEqual(len(r['issued']),2)
        self.assertEqual(len({x['password'] for x in r['issued']}),2)
        _,again,_=request('POST','/api/admin/credentials',{'action':'bulk-participants'},self.admin)
        self.assertEqual(again,{'issued':[],'skipped':2})
        participant=self.login(r['issued'][0]['password'])
        self.assertEqual(request('POST','/api/access/logout',{},participant)[0],200)
        self.assertEqual(request('GET','/api/event',cookie=participant)[0],401)

    def test_unverified_city_headers_are_ignored_and_rate_limit_uses_client_ip(self):
        with patch.dict(os.environ,{'KEVENOS_CF_LOCATION_HEADERS':'0'}):
            request('GET','/',headers={'CF-Ray':'fake','CF-Connecting-IP':'203.0.113.40','CF-IPCountry':'AE','CF-IPCity':'Spoofed'})
        _,r,_=request('GET','/api/admin/activity',cookie=self.admin)
        self.assertEqual(r['events'][0]['city'],'')
        hdr={'CF-Ray':'fake','CF-Connecting-IP':'203.0.113.40'}
        for _ in range(8): request('POST','/api/access/login',{'role':'guest','password':'wrong'},headers=hdr)
        self.assertEqual(request('POST','/api/access/login',{'role':'guest','password':'wrong'},headers=hdr)[0],429)
        self.assertEqual(request('POST','/api/access/login',{'role':'guest','password':'wrong'},headers={'CF-Ray':'fake','CF-Connecting-IP':'203.0.113.41'})[0],403)

    def test_cross_origin_cookie_mutations_and_expired_sessions_denied(self):
        self.assertEqual(request('POST','/api/desk/program',{'action':'announce','text':'Bad'},self.admin,{'Origin':'https://evil.example'})[0],403)
        with patch.object(S.time,'time',return_value=S.time.time()+5*3600):
            self.assertEqual(request('GET','/api/admin/credentials',cookie=self.admin)[0],401)

if __name__=='__main__': unittest.main()
