"""Admin creates a staff identity with explicit roles; roster remains authoritative."""
import unittest
import test_multi_role as M
import test_event_access as A
S=A.S

class StaffAccounts(unittest.TestCase):
    setUp=M.MultiRole.setUp
    request=M.MultiRole.request
    login=M.MultiRole.login
    issue=M.MultiRole.issue
    call=M.MultiRole.call

    def create(self, roles, **fields):
        return self.call('POST','/api/admin/credentials',{'action':'create-staff','label':'Staff Example','grants':roles,**fields})[0]

    def test_staff_creation_assigns_multiple_roles_atomically(self):
        result=self.create([{'role':'judge','station':2},{'role':'registration'},{'role':'organizer'}])
        self.assertEqual(result['credential']['account_type'],'staff')
        self.assertEqual({g['role'] for g in result['credential']['grants']},{'judge','registration','organizer'})
        me,headers=self.call('POST','/api/access/login',{'password':result['password']},cookie='separate-browser')
        self.assertEqual({d['role'] for d in me['dashboards']},{'judge','registration','organizer'})
        cookie=next(h for h in headers['set-cookie'] if h.startswith('pp_access=')).split(';')[0]
        self.call('GET','/api/access/me',cookie=cookie,dashboard='admin',status=403)
        self.assertEqual(self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')[0]['station'],2)
        accounts=self.call('GET','/api/admin/accounts')[0]['accounts']
        self.assertEqual(next(a for a in accounts if a['id']==result['credential']['id'])['account_type'],'staff')

    def test_invalid_or_duplicate_role_never_leaves_partial_account(self):
        self.issue('judge',station=1)
        before=S.AUTH_DB.execute('SELECT COUNT(*) FROM access_credentials').fetchone()[0]
        for grants in ([{'role':[]}],[{'role':{}}],[],[{'role':'participant','pair':1,'member':1}],[{'role':'guest'}],[{'role':'judge','station':1},{'role':'admin'}],[{'role':'registration','unexpected':True}]):
            self.call('POST','/api/admin/credentials',{'action':'create-staff','label':'Invalid staff','grants':grants},status=400)
            self.assertEqual(S.AUTH_DB.execute('SELECT COUNT(*) FROM access_credentials').fetchone()[0],before)

    def test_rehearsal_shared_creation_requires_explicit_confirmation(self):
        self.call('POST','/api/admin/workspace',{'mode':'rehearsal','confirm':'REHEARSAL'})
        body={'action':'create-staff','label':'Practice staff','grants':[{'role':'organizer'},{'role':'judge','station':1}]}
        self.call('POST','/api/admin/credentials',body,status=400)
        created=self.call('POST','/api/admin/credentials',dict(body,confirm_shared_roles=True))[0]
        self.assertEqual({g['dataset'] for g in created['credential']['grants']},{'global','rehearsal'})
        self.call('POST','/api/admin/workspace',{'mode':'live','confirm':'LIVE'})
        me,_=self.call('POST','/api/access/login',{'password':created['password']},cookie='separate-browser')
        self.assertEqual([d['role'] for d in me['dashboards']],['organizer'])

    def test_staff_creation_is_admin_only(self):
        issued=self.issue('registration');cookie=self.login('registration',issued['password'])
        self.call('POST','/api/admin/credentials',{'action':'create-staff','label':'Forbidden','grants':[{'role':'admin'}]},cookie=cookie,dashboard='registration',status=403)

    def test_password_controls_use_all_datasets_not_visible_roles(self):
        judge=self.issue('judge',station=3);aid=judge['credential']['id']
        self.call('POST','/api/admin/workspace',{'mode':'rehearsal','confirm':'REHEARSAL'})
        self.call('POST','/api/admin/accounts',{'action':'roles','id':aid,'revision':1,
            'reason':'Test same judge in practice','grants':[{'role':'judge','station':3}]})
        practice=self.create([{'role':'judge','station':2}])
        records=self.call('GET','/api/admin/credentials')[0]['credentials']
        self.assertFalse(next(c for c in records if c['id']==aid)['can_manage_password'])
        self.assertTrue(next(c for c in records if c['id']==practice['credential']['id'])['can_manage_password'])
        self.call('POST','/api/admin/credentials',{'action':'reset','id':aid},status=409)

    def test_link_preserves_quiz_history_in_live_and_unopened_rehearsal(self):
        import json
        for dataset in ('live','rehearsal'):
            for position in ('source','retained'):
                with self.subTest(dataset=dataset, position=position):
                    retained=self.issue('organizer');source=self.issue('registration')
                    retained_id=retained['credential']['id'];source_id=source['credential']['id']
                    touched=source_id if position=='source' else retained_id
                    if dataset=='rehearsal':
                        S.WORKSPACE.switch('rehearsal','REHEARSAL')
                    S.DB.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)",('event_quiz_v1',json.dumps({'members':{touched:{'name':'Example'}}})))
                    S.DB.commit()
                    if dataset=='rehearsal':
                        S.WORKSPACE.switch('live','LIVE')
                        S.WORKSPACE.rehearsal.close();S.WORKSPACE.rehearsal=None
                    before=[tuple(r) for r in S.AUTH_DB.execute('SELECT id,account_id,active,password_hash FROM access_credentials ORDER BY id')]
                    result,_=self.call('POST','/api/admin/accounts',{'action':'link','id':retained_id,'revision':1,
                        'source_id':source_id,'source_revision':1,'reason':'Same verified person',
                        'confirm':'LINK AND RETIRE SOURCE PASSWORD'},status=400)
                    self.assertIn('quiz activity',result['error'])
                    self.assertEqual(before,[tuple(r) for r in S.AUTH_DB.execute('SELECT id,account_id,active,password_hash FROM access_credentials ORDER BY id')])
                    self.assertEqual(S.AUTH_DB.execute('SELECT revision FROM person_accounts WHERE id=?',(retained_id,)).fetchone()[0],1)

    def test_link_protects_quiz_reset_archives_in_both_workspaces(self):
        import json
        for dataset in ('live','rehearsal'):
            with self.subTest(dataset=dataset):
                retained=self.issue('organizer');source=self.issue('registration')
                source_id=source['credential']['id']
                if dataset=='rehearsal':S.WORKSPACE.switch('rehearsal','REHEARSAL')
                archive=[{'id':'saved-reset','at':1,'reason':'Test reset archive','reset_rev':4,
                          'state':{'answers':{source_id:{'run-old':{'key':'A'}}}}}]
                S.DB.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)',('event_quiz_reset_archive_v1',json.dumps(archive)));S.DB.commit()
                if dataset=='rehearsal':
                    S.WORKSPACE.switch('live','LIVE');S.WORKSPACE.rehearsal.close();S.WORKSPACE.rehearsal=None
                result,_=self.call('POST','/api/admin/accounts',{'action':'link','id':retained['credential']['id'],'revision':1,
                  'source_id':source_id,'source_revision':1,'reason':'Same verified person',
                  'confirm':'LINK AND RETIRE SOURCE PASSWORD'},status=400)
                self.assertIn('quiz activity',result['error'])
                self.assertEqual(S.AUTH_DB.execute('SELECT active FROM access_credentials WHERE id=?',(source_id,)).fetchone()[0],1)

    def test_link_protects_archive_created_by_real_reset(self):
        import event_quiz
        retained=self.issue('organizer');source=self.issue('registration')
        source_id=source['credential']['id']
        state=event_quiz.load(S)
        state['answers']={source_id:{'old-run':{'key':'A'}}}
        S.kv_set(event_quiz.KEY,state)
        before=self.call('GET','/api/desk/quiz')[0]
        cleared=self.call('POST','/api/desk/quiz',{'action':'reset','rev':before['rev'],
            'confirm':'RESET QUIZ','reason':'Restart disposable test'})[0]
        self.assertEqual(cleared['reset_archives'][0]['answer_count'],1)
        self.assertEqual(event_quiz.load(S)['answers'],{})
        result,_=self.call('POST','/api/admin/accounts',{'action':'link','id':retained['credential']['id'],
            'revision':1,'source_id':source_id,'source_revision':1,'reason':'Same verified person',
            'confirm':'LINK AND RETIRE SOURCE PASSWORD'},status=400)
        self.assertIn('quiz activity',result['error'])
        self.assertEqual(S.AUTH_DB.execute('SELECT active FROM access_credentials WHERE id=?',(source_id,)).fetchone()[0],1)

if __name__=='__main__':unittest.main()
