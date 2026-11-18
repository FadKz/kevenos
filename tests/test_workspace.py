"""Rehearsal must never borrow live event data or participant authority."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import test_api as T
import event_access as A

S = T.S


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.previous = {k: getattr(S, k, None) for k in ('DB','DATA','CFG','CONTENT','AUTH_DB','WORKSPACE')}
        self.addCleanup(self.restore)
        S.DATA = self.temp.name
        S.CFG = S.load_config(str(Path(S.DATA)/'config.json'))
        S.DB = S.open_db(str(Path(S.DATA)/'judging.db'))
        S.CONTENT = S.bundled_content()
        S.DB.execute("INSERT INTO roster(pair,partner,p1,p2) VALUES(1,'Live business','Live Alice','Live Bob')")
        S.DB.commit()

    def restore(self):
        controller = getattr(S, 'WORKSPACE', None)
        if controller: controller.close()
        else: S.DB.close()
        for k,v in self.previous.items(): setattr(S,k,v)

    def start(self):
        import event_workspace as W
        return W.initialize(S, str(Path(S.DATA)/'judging.db'), S.DATA)

    def test_non_default_practice_settings_survive_reset_restart_and_live_switch(self):
        import event_config as C
        w = self.start()
        live_config = C.config(S)
        live_members = C.roster_row(S, 1)['members']
        w.switch('rehearsal', 'REHEARSAL')
        w.reset('RESET REHEARSAL', configuration={'participant_count': 11, 'team_size': 3, 'judge_count': 3})
        config = C.config(S)
        self.assertEqual((w.status()['summary']['participants'], w.status()['summary']['registered_pairs']), (11, 4))
        self.assertEqual(len(C.roster_row(S, 4)['members']), 2)
        w.reset('RESET REHEARSAL')
        self.assertEqual(C.config(S), config)
        w.close()
        S.WORKSPACE = None
        S.AUTH_DB = None
        S.DATA = self.temp.name
        S.DB = S.open_db(str(Path(self.temp.name)/'judging.db'))
        w = self.start()
        self.assertEqual(w.mode, 'rehearsal')
        self.assertEqual(C.config(S), config)
        w.switch('live', 'LIVE')
        self.assertEqual(C.config(S), live_config)
        self.assertEqual(C.roster_row(S, 1)['members'], live_members)

    def test_rehearsal_seed_and_switch_preserve_live(self):
        live_content = dict(S.CONTENT)
        live_content['content_note'] = 'Private live content'
        Path(S.DATA,'content.json').write_text(json.dumps(live_content))
        live_bytes = Path(S.DATA,'content.json').read_bytes()
        live = S.DB
        live.execute("INSERT INTO sheets(kind,pair,station,round,judge,ticks,notes,created) VALUES('round',1,1,1,'Live Judge','[true,false]','Private score notes',1)")
        live.commit()
        live_sheets = [tuple(r) for r in live.execute('SELECT * FROM sheets')]
        w = self.start()
        self.assertEqual(w.mode, 'live')
        context = w.context
        result = w.switch('rehearsal', 'REHEARSAL')
        self.assertNotEqual(w.context, context)
        self.assertEqual(result['summary']['registered_pairs'], 30)
        self.assertEqual(result['summary']['participants'], 60)
        self.assertEqual(result['summary']['round_sheets'], 8)
        self.assertEqual(result['summary']['issued_participants'], 0)
        self.assertNotIn('Live Alice', json.dumps([dict(r) for r in S.DB.execute('SELECT * FROM roster')]))
        self.assertNotIn('Private live content', json.dumps(S.CONTENT))
        self.assertEqual(S.clock_public()['round'], 2)
        self.assertEqual(S.mode(), 'desk')
        self.assertEqual(S.public_mode(), 'off')
        w.switch('live', 'LIVE')
        self.assertIs(S.DB, live)
        self.assertEqual([tuple(r) for r in live.execute('SELECT * FROM sheets')],live_sheets)
        self.assertEqual(S.DB.execute('SELECT p1 FROM roster').fetchone()[0], 'Live Alice')
        self.assertEqual(Path(S.DATA,'content.json').read_bytes(), live_bytes)
        self.assertEqual(S.CONTENT['content_note'], 'Private live content')

    def create(self, role, **kwargs):
        with A.auth_db(S):
            return A.create_credential(S, role, 'Synthetic '+role, **kwargs)

    def token(self, item):
        c = item['credential']
        with A.auth_db(S):
            token, _ = A.mint_session(S,c['role'],c['id'],c['label'])
        return token

    def test_credentials_and_sessions_are_isolated_and_staff_survive(self):
        w = self.start()
        live = self.create('participant',pair=1,member=1)
        admin = self.create('admin')
        desk = self.create('desk')
        guest = self.create('guest')
        live_token = self.token(live)
        staff_tokens = [self.token(c) for c in (admin,desk,guest)]
        w.switch('rehearsal','REHEARSAL')
        self.assertIsNone(A.session(S,{'Cookie':'pp_access='+live_token}))
        for token in staff_tokens: self.assertIsNotNone(A.session(S,{'Cookie':'pp_access='+token}))
        practice = self.create('participant',pair=1,member=1)
        self.assertEqual(practice['credential']['label'],'PracticePerson01A')
        self.assertEqual(practice['credential']['dataset'],'rehearsal')
        practice_token = self.token(practice)
        self.assertEqual(A.session(S,{'Cookie':'pp_access='+practice_token})['role'],'participant')
        self.assertEqual(S.AUTH_DB.execute("SELECT count(*) FROM access_credentials WHERE role='participant'").fetchone()[0],2)
        w.switch('live','LIVE')
        self.assertIsNone(A.session(S,{'Cookie':'pp_access='+practice_token}))
        self.assertEqual(S.AUTH_DB.execute('SELECT active FROM access_credentials WHERE id=?',(live['credential']['id'],)).fetchone()[0],1)
        for token in staff_tokens: self.assertIsNotNone(A.session(S,{'Cookie':'pp_access='+token}))

    def test_reset_only_replaces_rehearsal_data_and_credentials(self):
        w = self.start()
        live = self.create('participant',pair=1,member=1)
        admin = self.create('admin')
        admin_token = self.token(admin)
        w.switch('rehearsal','REHEARSAL')
        practice = self.create('participant',pair=1,member=1)
        token = self.token(practice)
        Path(S.DATA,'content-history').mkdir()
        Path(S.DATA,'content-history','test.json').write_text('{}')
        modified = dict(S.CONTENT,content_note='Old practice edit')
        Path(S.DATA,'content.json').write_text(json.dumps(modified))
        S.DB.execute('DELETE FROM sheets')
        S.DB.commit()
        context = w.context
        result = w.reset('RESET REHEARSAL')
        self.assertNotEqual(w.context,context)
        self.assertEqual(result['summary']['round_sheets'],8)
        self.assertEqual(result['summary']['issued_participants'],0)
        self.assertFalse(Path(S.DATA,'content-history').exists())
        self.assertNotIn('Old practice edit',json.dumps(S.CONTENT))
        self.assertIsNone(A.session(S,{'Cookie':'pp_access='+token}))
        self.assertIsNotNone(A.session(S,{'Cookie':'pp_access='+admin_token}))
        self.assertIsNotNone(S.AUTH_DB.execute('SELECT 1 FROM access_credentials WHERE id=?',(live['credential']['id'],)).fetchone())
        w.switch('live','LIVE')
        with self.assertRaises(ValueError): w.reset('RESET REHEARSAL')

    def test_restart_preserves_selection_context_and_practice_edits(self):
        import event_workspace as W
        w = self.start()
        w.switch('rehearsal','REHEARSAL')
        S.DB.execute("UPDATE roster SET p1='Practice edit' WHERE pair=1")
        S.DB.commit()
        context = w.context
        w.close()
        S.WORKSPACE = None
        S.DATA = self.temp.name
        S.DB = S.open_db(str(Path(S.DATA)/'judging.db'))
        w = W.initialize(S,str(Path(S.DATA)/'judging.db'),S.DATA)
        self.assertEqual(w.context,context)
        self.assertEqual(w.mode,'rehearsal')
        self.assertEqual(S.DB.execute('SELECT p1 FROM roster WHERE pair=1').fetchone()[0],'Practice edit')

    def test_mode_switch_denies_desk_and_participant(self):
        import event_workspace as W
        from unittest.mock import patch
        w = self.start()
        class Handler:
            def __init__(self, token): self.headers={'Cookie':'pp_access='+token}
            def send(self,status,data): self.status,self.data=status,data
        for role,kwargs in [('desk',{}),('participant',{'pair':1,'member':1})]:
            h=Handler(self.token(self.create(role,**kwargs)))
            with patch.object(S,'hosted',return_value=True):
                W.handle_post(S,h,'/api/admin/workspace',{'mode':'rehearsal','confirm':'REHEARSAL'})
            self.assertEqual(h.status,403)
            self.assertEqual(w.mode,'live')

    def test_pending_switch_waits_for_readers_and_blocks_new_readers(self):
        w = self.start()
        first_entered = threading.Event()
        release = threading.Event()
        writer_finished = threading.Event()
        second_finished = threading.Event()
        order=[]
        def first():
            with w.read():
                first_entered.set()
                release.wait(3)
        def writer():
            w.switch('rehearsal','REHEARSAL')
            order.append('writer'); writer_finished.set()
        def second():
            with w.read():
                self.assertEqual(w.mode,'rehearsal')
                order.append('reader'); second_finished.set()
        t1=threading.Thread(target=first); t1.start()
        self.assertTrue(first_entered.wait(1))
        t2=threading.Thread(target=writer); t2.start()
        deadline=time.monotonic()+2
        while not w.guard.waiting and time.monotonic()<deadline: time.sleep(.005)
        self.assertEqual(w.guard.waiting,1)
        t3=threading.Thread(target=second); t3.start()
        self.assertFalse(writer_finished.wait(.03))
        self.assertFalse(second_finished.is_set())
        release.set()
        for thread in (t1,t2,t3): thread.join(3); self.assertFalse(thread.is_alive())
        self.assertEqual(order,['writer','reader'])

    def test_reset_keeps_new_sqlite_wal_intact_after_restart(self):
        import event_workspace as W
        w=self.start(); w.switch('rehearsal','REHEARSAL')
        w.reset('RESET REHEARSAL')
        S.DB.execute("UPDATE roster SET p1='After reset' WHERE pair=1")
        S.DB.commit()
        w.close(); S.WORKSPACE=None
        S.DATA=self.temp.name
        S.DB=S.open_db(str(Path(S.DATA)/'judging.db'))
        W.initialize(S,str(Path(S.DATA)/'judging.db'),S.DATA)
        self.assertEqual(S.DB.execute('SELECT p1 FROM roster WHERE pair=1').fetchone()[0],'After reset')

    def access(self, method, path, body=None, token=None):
        from unittest.mock import patch
        class Handler:
            headers={'Cookie':'pp_access='+token} if token else {}
            client_address=('127.0.0.1',1234)
            def send(self,status,data,**kwargs): self.status,self.data=status,data
        handler=Handler()
        with patch.object(S,'hosted',return_value=True):
            if method=='GET': A.handle_get(S,handler,path,{})
            else: A.handle_post(S,handler,path,body)
        return handler.status,handler.data

    def test_rehearsal_password_and_management_cannot_touch_live_identity(self):
        w=self.start()
        live=self.create('participant',pair=1,member=1)
        admin=self.token(self.create('admin'))
        w.switch('rehearsal','REHEARSAL')
        practice=self.create('participant',pair=1,member=1)
        code,data=self.access('GET','/api/admin/credentials',token=admin)
        self.assertEqual(code,200)
        ids=[r['id'] for r in data['credentials']]
        self.assertIn(practice['credential']['id'],ids)
        self.assertNotIn(live['credential']['id'],ids)
        for action in ('reset','revoke'):
            self.assertEqual(self.access('POST','/api/admin/credentials',{'action':action,'id':live['credential']['id']},admin)[0],409)
        self.assertEqual(self.access('POST','/api/access/login',{'role':'participant','password':live['password']})[0],403)
        code,data=self.access('POST','/api/access/login',{'role':'participant','password':practice['password']})
        self.assertEqual(code,200)
        self.assertEqual(data['role'],'participant')
        self.assertEqual(self.access('POST','/api/admin/credentials',{'action':'bulk-participants'},admin)[1]['skipped'],1)
        self.assertEqual(w.status()['summary']['issued_participants'],60)
        w.switch('live','LIVE')
        self.assertEqual(self.access('POST','/api/access/login',{'role':'participant','password':practice['password']})[0],403)
        code,data=self.access('POST','/api/access/login',{'role':'participant','password':live['password']})
        self.assertEqual(code,200)
        self.assertEqual(data['role'],'participant')
        self.assertEqual(w.status()['summary']['issued_participants'],1)

    def test_migration_scopes_existing_participants_judges_and_staff(self):
        import sqlite3
        db=sqlite3.connect(':memory:'); self.addCleanup(db.close)
        db.row_factory=sqlite3.Row
        db.executescript('''
            CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,actor TEXT,action TEXT,detail TEXT);
            CREATE TABLE role_sessions(digest TEXT PRIMARY KEY,role TEXT,station INTEGER,judge TEXT,expires REAL,credential_id TEXT DEFAULT '',legacy_gate INTEGER DEFAULT 0);
            INSERT INTO role_sessions(digest,role) VALUES('a','admin'),('d','desk'),('j','judge'),('p','participant');
            CREATE TABLE access_credentials(id TEXT PRIMARY KEY,label TEXT,role TEXT,pair INTEGER,member INTEGER,lookup TEXT UNIQUE,password_hash TEXT,active INTEGER DEFAULT 1,created REAL,last_used REAL);
            INSERT INTO access_credentials(id,role,pair,member) VALUES('a','admin',0,0),('p','participant',1,1);
            CREATE UNIQUE INDEX participant_access ON access_credentials(pair,member) WHERE role='participant' AND active=1;
        ''')
        A.migrate(db)
        self.assertEqual(dict(db.execute('SELECT digest,dataset FROM role_sessions')),{'a':'global','d':'global','j':'live','p':'live'})
        self.assertEqual(dict(db.execute('SELECT id,dataset FROM access_credentials')),{'a':'global','p':'live'})
        db.execute("INSERT INTO access_credentials(id,role,pair,member,dataset) VALUES('practice','participant',1,1,'rehearsal')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO access_credentials(id,role,pair,member,dataset) VALUES('duplicate','participant',1,1,'live')")

    def test_reset_failure_restores_practice_data_and_accepts_next_switch(self):
        from unittest.mock import patch
        w=self.start(); w.switch('rehearsal','REHEARSAL')
        S.DB.execute("UPDATE roster SET p1='Before failed reset' WHERE pair=1"); S.DB.commit()
        with patch.object(w,'_seed',side_effect=OSError('Synthetic seed failure')):
            with self.assertRaises(OSError): w.reset('RESET REHEARSAL')
        self.assertEqual(S.DB.execute('SELECT p1 FROM roster WHERE pair=1').fetchone()[0],'Before failed reset')
        self.assertEqual(w.switch('live','LIVE')['mode'],'live')

    def test_reset_directory_failure_leaves_usable_workspace(self):
        from unittest.mock import patch
        w=self.start(); w.switch('rehearsal','REHEARSAL')
        with patch.object(Path,'rename',side_effect=OSError('Synthetic rename failure')):
            with self.assertRaises(OSError): w.reset('RESET REHEARSAL')
        self.assertEqual(S.DB.execute('SELECT count(*) FROM roster').fetchone()[0],30)

    def test_readiness_counts_stations_and_unused_invites(self):
        w=self.start(); w.switch('rehearsal','REHEARSAL')
        with S.AUTH_DB:
            for digest in ('session-a','session-b'):
                S.AUTH_DB.execute("INSERT INTO role_sessions(digest,role,station,judge,expires,dataset) VALUES(?,'judge',1,'PracticeJudge01',?,'rehearsal')",(digest,time.time()+600))
        with S.DB:
            S.DB.execute("INSERT INTO judge_invites(station,digest,judge,expires,used) VALUES(1,'used','PracticeJudge01',?,1)",(time.time()+600,))
            S.DB.execute("INSERT INTO judge_invites(station,digest,judge,expires,used) VALUES(2,'ready','PracticeJudge02',?,0)",(time.time()+600,))
        self.assertEqual(w.status()['summary']['judge_sessions'],1)
        self.assertEqual(w.status()['summary']['judge_invites'],1)

    def test_practice_cannot_change_global_credentials_or_read_live_activity(self):
        w=self.start()
        staff=self.create('admin'); admin=self.token(staff)
        with S.AUTH_DB:
            for dataset,label in [('live','Private live action'),('rehearsal','Practice action')]:
                S.AUTH_DB.execute("INSERT INTO visitor_activity(ts,visitor_id,credential_id,label,role,ip,country,region,city,action,path,status,dataset) VALUES(?,'visitor','',?,'admin','','','','','view','/admin',200,?)",(time.time(),label,dataset))
        w.switch('rehearsal','REHEARSAL')
        for body in ({'action':'create','role':'desk','label':'Staff'}, {'action':'reset','id':staff['credential']['id']}, {'action':'revoke','id':staff['credential']['id']}):
            code,data=self.access('POST','/api/admin/credentials',body,admin)
            self.assertEqual(code,409,data)
        code,data=self.access('GET','/api/admin/activity',token=admin)
        self.assertEqual(code,200)
        self.assertEqual(data['summary']['views'],1)
        self.assertEqual([r['label'] for r in data['events']],['Practice action'])
