"""HTTP acceptance tests for person accounts, task contexts and role revocation."""
import test_access_profiles as base
import test_event_access as access
S = access.S

class MultiRole(base.unittest.TestCase):
    setUp=base.AccessProfiles.setUp
    request=base.AccessProfiles.request
    login=base.AccessProfiles.login
    issue=base.AccessProfiles.issue
    profile=base.AccessProfiles.profile
    roster=base.AccessProfiles.roster
    # Reuse setup/helpers, without inheriting the legacy test cases below.
    def call(self, method, path, body=None, cookie=None, dashboard=None, status=200):
        cookie = cookie or self.admin
        headers = {'X-Event-Context': S.WORKSPACE.context}
        if dashboard: headers['X-Dashboard'] = dashboard
        if method == 'POST' and path != '/api/access/login':
            code, me, _ = access.request('GET','/api/access/me',cookie=cookie,headers=headers,auto_context=False)
            headers['X-Access-Context'] = me.get('context','')
        code, data, response = access.request(method,path,body,cookie,headers,auto_context=False)
        self.assertEqual(code,status,data)
        return data,response

    def grants(self, credential, roles):
        return self.call('POST','/api/admin/accounts',{'action':'roles','id':credential['id'],
            'revision':1,'reason':'Assign event duties','grants':roles})[0]

    def test_password_only_login_and_combined_dashboards(self):
        issued=self.issue('judge',station=3)
        self.grants(issued['credential'],[{'role':'judge','station':3},{'role':'registration'}])
        me,headers=self.call('POST','/api/access/login',{'password':issued['password']},cookie='')
        self.assertEqual({d['role'] for d in me['dashboards']},{'judge','registration'})
        self.assertEqual(me['next'],'/home')
        cookie=next(v for v in headers['set-cookie'] if v.startswith('pp_access=')).split(';')[0]
        judge,_=self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')
        registration,_=self.call('GET','/api/access/me',cookie=cookie,dashboard='registration')
        self.assertEqual(judge['station'],3)
        self.assertEqual(registration['role'],'registration')
        self.call('GET','/api/state',cookie=cookie,dashboard='judge')
        self.call('GET','/api/registration',cookie=cookie,dashboard='registration')
        self.call('GET','/api/desk/sheets',cookie=cookie,dashboard='registration',status=403)
        self.call('GET','/api/admin/credentials',cookie=cookie,dashboard='judge',status=403)
        self.call('GET','/api/access/me',cookie=cookie,dashboard='admin',status=403)
        self.assertEqual(self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')[0]['station'],3)

    def test_organizer_scoring_are_distinct_authorities(self):
        issued=self.issue('desk')
        self.grants(issued['credential'],[{'role':'organizer'}])
        cookie=self.login('desk',issued['password'])
        state,_=self.call('GET','/api/desk/state',cookie=cookie,dashboard='organizer')
        self.assertNotIn('view_key',state)
        for row in state['stations']:
            self.assertNotIn('ticks_live',row);self.assertNotIn('score',row)
        self.call('POST','/api/desk/program',{'action':'announce','text':'Welcome'},cookie,'organizer')
        self.call('GET','/api/desk/sheets',cookie=cookie,dashboard='organizer',status=403)
        self.call('POST','/api/desk/reveal',{'mode':'all'},cookie,'organizer',status=403)
        self.call('POST','/api/desk/clock',{'action':'start'},cookie,'organizer',status=403)

    def test_revocation_invalidates_existing_session(self):
        issued=self.issue('registration')
        cookie=self.login('registration',issued['password'])
        self.grants(issued['credential'],[{'role':'organizer'}])
        self.call('GET','/api/registration',cookie=cookie,dashboard='registration',status=401)

    def test_guest_cannot_gain_staff_roles(self):
        S.event_access.seed_guest(S)
        self.call('POST','/api/admin/accounts',{'action':'roles','id':'guest','revision':1,
          'reason':'Invalid guest escalation','grants':[{'role':'admin'}]},status=400)


    def test_new_admin_gets_only_admin_and_no_operational_escape(self):
        issued=self.issue('admin')
        cookie=self.login('admin',issued['password'])
        me,_=self.call('GET','/api/access/me',cookie=cookie,dashboard='home')
        self.assertEqual([d['role'] for d in me['dashboards']],['admin'])
        self.call('POST','/api/desk/clock',{'action':'start'},cookie,'admin',status=403)
        self.call('POST','/api/desk/clock',{'action':'start'},cookie,status=403)
        self.call('GET','/api/access/me',cookie=cookie,dashboard='scoring',status=403)
        self.call('GET','/api/public',cookie=cookie,dashboard='home')
        self.call('GET','/api/event',cookie=cookie,dashboard='home',status=403)

    def test_contexts_are_tab_bound_and_legacy_union_is_not_a_dashboard(self):
        issued=self.issue('judge',station=3)
        self.grants(issued['credential'],[{'role':'judge','station':3},{'role':'registration'}])
        cookie=self.login('judge',issued['password'])
        judge,_=self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')
        reg,_=self.call('GET','/api/access/me',cookie=cookie,dashboard='registration')
        self.assertNotEqual(judge['context'],reg['context'])
        code,_,_=access.request('POST','/api/sync',{},cookie,{'X-Dashboard':'judge','X-Access-Context':reg['context'],'X-Event-Context':S.WORKSPACE.context},auto_context=False)
        self.assertEqual(code,409)
        self.call('GET','/api/access/me',cookie=self.admin,dashboard='desk',status=403)

    def test_link_keeps_one_password_and_retires_source(self):
        judge=self.issue('judge',station=3);registration=self.issue('registration')
        cookie=self.login('registration',registration['password'])
        self.call('POST','/api/admin/accounts',{'action':'link','id':judge['credential']['id'],'revision':1,
          'source_id':registration['credential']['id'],'source_revision':1,'reason':'Same verified person',
          'confirm':'LINK AND RETIRE SOURCE PASSWORD'})
        self.call('POST','/api/access/login',{'password':registration['password']},status=403)
        self.call('GET','/api/registration',cookie=cookie,dashboard='registration',status=401)
        me,_=self.call('POST','/api/access/login',{'password':judge['password']})
        self.assertEqual({d['role'] for d in me['dashboards']},{'judge','registration'})

    def test_assignment_uniqueness_and_old_draft_identity(self):
        judge=self.issue('judge',station=3);reg=self.issue('registration')
        self.call('POST','/api/admin/accounts',{'action':'roles','id':reg['credential']['id'],'revision':1,
          'reason':'Invalid duplicate','grants':[{'role':'judge','station':3}]},status=400)
        cookie=self.login('judge',judge['password'])
        before=self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')[0]
        self.grants(judge['credential'],[{'role':'judge','station':3},{'role':'registration'}])
        cookie=self.login('judge',judge['password'])
        after=self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')[0]
        self.assertNotEqual(before['draft_key'],after['draft_key'])

    def test_rehearsal_cannot_change_global_roles_or_link_passwords(self):
        issued=self.issue('registration')
        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        self.call('POST','/api/admin/accounts',{'action':'roles','id':issued['credential']['id'],'revision':1,
          'reason':'Unsafe global change','grants':[{'role':'admin'}]},status=400)
        self.call('POST','/api/admin/accounts',{'action':'link','id':issued['credential']['id'],'revision':1,
          'reason':'Unsafe global link','source_id':'anything'},status=400)

    def test_rehearsal_confirmed_shared_roles_keep_judging_and_live_assignments(self):
        issued=self.issue('judge',station=3);aid=issued['credential']['id']
        self.grants(issued['credential'],[{'role':'judge','station':3},{'role':'registration'}])
        cookie=self.login('judge',issued['password'])
        db=S.event_access.auth_db(S)
        password=db.execute('SELECT password_hash FROM access_credentials WHERE id=?',(aid,)).fetchone()[0]
        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        body={'action':'roles','id':aid,'revision':2,'reason':'Cover arrivals and programme',
              'grants':[{'role':'judge','station':4},{'role':'organizer'},{'role':'registration'}]}
        for invalid in (None,False,'true',1):
            self.call('POST','/api/admin/accounts',dict(body,confirm_shared_roles=invalid),status=400)
        self.call('GET','/api/registration',cookie=cookie,dashboard='registration')
        self.call('POST','/api/admin/accounts',dict(body,confirm_shared_roles=True))
        self.assertEqual(S.event_access.active_dataset(S),'rehearsal')
        db=S.event_access.auth_db(S)
        self.assertEqual(db.execute('SELECT password_hash FROM access_credentials WHERE id=?',(aid,)).fetchone()[0],password)
        self.assertEqual(db.execute("SELECT station FROM account_grants WHERE account_id=? AND role='judge' AND dataset='live'",(aid,)).fetchone()[0],3)
        self.call('GET','/api/registration',cookie=cookie,dashboard='registration',status=401)
        cookie=self.login('judge',issued['password'])
        me,_=self.call('GET','/api/access/me',cookie=cookie,dashboard='home')
        self.assertEqual({d['role'] for d in me['dashboards']},{'judge','organizer','registration'})
        self.assertEqual(self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')[0]['station'],4)
        self.call('GET','/api/registration',cookie=cookie,dashboard='registration')
        self.call('GET','/api/desk/state',cookie=cookie,dashboard='organizer')
        self.call('GET','/api/admin/accounts',cookie=cookie,dashboard='organizer',status=403)
        self.call('POST','/api/admin/accounts',dict(body,revision=2,confirm_shared_roles=True),cookie=cookie,dashboard='registration',status=403)
        self.call('POST','/api/admin/accounts',dict(body,confirm_shared_roles=True),status=409)
        S.WORKSPACE.switch('live','LIVE')
        cookie=self.login('judge',issued['password'])
        self.assertEqual(self.call('GET','/api/access/me',cookie=cookie,dashboard='judge')[0]['station'],3)

    def test_shared_role_audit_survives_rehearsal_reset(self):
        import json
        issued=self.issue('judge',station=3);aid=issued['credential']['id']
        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        body={'action':'roles','id':aid,'revision':1,'reason':'Confirmed shared staff coverage '+('x'*220),
              'grants':[{'role':'judge','station':4},{'role':'organizer'},{'role':'registration'}],
              'confirm_shared_roles':True}
        self.call('POST','/api/admin/accounts',body)
        db=S.event_access.auth_db(S)
        row=db.execute("SELECT * FROM audit WHERE action='account-shared-roles' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        detail=json.loads(row['detail'])
        self.assertTrue(detail['shared_roles_confirmed'])
        self.assertEqual(detail['dataset'],'rehearsal')
        self.assertEqual(detail['reason'],body['reason'])
        self.assertEqual(set(detail['after']),{'organizer','registration'})
        self.assertEqual(row['effective_role'],'admin')
        self.call('POST','/api/admin/accounts',dict(body,revision=2,confirm_shared_roles='true',grants=[{'role':'judge','station':4}]),status=400)
        self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='account-shared-roles'").fetchone()[0],1)
        S.WORKSPACE.reset('RESET REHEARSAL')
        self.assertEqual(db.execute('SELECT detail FROM audit WHERE id=?',(row['id'],)).fetchone()[0],row['detail'])

    def test_scoring_denies_registration_quiz_and_admin(self):
        issued=self.issue('scoring');cookie=self.login('scoring',issued['password'])
        self.call('GET','/api/desk/scores',cookie=cookie,dashboard='scoring')
        self.call('GET','/api/desk/export.csv',cookie=cookie,dashboard='scoring')
        for path in ('/api/registration','/api/admin/accounts','/api/desk/quiz'):
            self.call('GET',path,cookie=cookie,dashboard='scoring',status=403)
        self.call('POST','/api/desk/sheet',{'id':1,'action':'void'},cookie,'scoring',status=400)

    def test_profile_requires_admin_context_and_tracks_target_revision(self):
        issued=self.issue('judge',station=3)
        self.call('POST','/api/access/impersonate',{'role':'judge','station':3},self.admin,'registration',status=403)
        profile,_=self.call('POST','/api/access/impersonate',{'role':'judge','station':3,'credential_id':issued['credential']['id'],'mode':'support'},dashboard='admin')
        self.grants(issued['credential'],[{'role':'registration'}])
        code,_,_=access.request('GET','/api/access/me',cookie=self.admin,headers={'X-Profile-Token':profile['profile_token']},auto_context=False)
        self.assertEqual(code,401)

    def test_assistant_catalog_matches_active_dashboard(self):
        import event_assistant
        self.assertNotIn('final.publish',event_assistant.commands('organizer'))
        self.assertNotIn('program.announce',event_assistant.commands('scoring'))
        self.assertNotIn('event.clock',event_assistant.commands('administration'))
        self.assertIn('event.configure',event_assistant.commands('administration'))

    def test_registration_qr_stays_participant_only_and_revokes_on_grant_change(self):
        self.roster()
        person=self.issue('participant',pair=1,member=1)
        self.grants(person['credential'],[{'role':'participant','pair':1,'member':1},{'role':'judge','station':3}])
        reg=self.issue('registration');cookie=self.login('registration',reg['password'])
        state,_=self.call('GET','/api/registration',cookie=cookie,dashboard='registration')
        self.call('POST','/api/registration',{'action':'reset','revision':state['revision'],'pair':1,'member':1},cookie,'registration',status=409)
        qr,_=self.call('POST','/api/registration',{'action':'qr','revision':state['revision'],'pair':1,'member':1},cookie,'registration')
        code=qr['link'].split('#')[1]
        result,headers=self.call('POST','/api/access/registration-link',{'code':code},cookie='pp_access=invalid')
        limited=next(v for v in headers['set-cookie'] if v.startswith('pp_access=')).split(';')[0]
        me,_=self.call('GET','/api/access/me',cookie=limited,dashboard='home')
        self.assertEqual([d['role'] for d in me['dashboards']],['participant'])
        self.call('GET','/api/state',cookie=limited,dashboard='judge',status=403)
        qr,_=self.call('POST','/api/registration',{'action':'qr','revision':state['revision'],'pair':1,'member':1},cookie,'registration')
        self.call('POST','/api/admin/accounts',{'action':'roles','id':person['credential']['id'],'revision':2,
          'reason':'Remove judging duty','grants':[{'role':'participant','pair':1,'member':1}]})
        self.call('POST','/api/access/registration-link',{'code':qr['link'].split('#')[1]},cookie='pp_access=invalid',status=403)

    def test_admin_only_cannot_use_legacy_participant_actions(self):
        issued=self.issue('admin')
        cookie=self.login('admin',issued['password'])
        for action in ('release','post_news','nominations'):
            self.call('POST','/api/desk/participant-workspace',{'action':action},cookie=cookie,status=403)

    def test_scoped_profiles_can_return_to_admin(self):
        for role in ('organizer','scoring','desk'):
            child,_=self.call('POST','/api/access/impersonate',{'role':role,'mode':'preview','reason':'Verify navigation'})
            headers={'X-Event-Context':S.WORKSPACE.context,'X-Profile-Token':child['profile_token']}
            code,me,_=access.request('GET','/api/access/me',cookie=self.admin,headers=headers,auto_context=False)
            self.assertEqual(code,200,me)
            headers['X-Access-Context']=me['context']
            code,data,_=access.request('POST','/api/access/impersonate/end',{},self.admin,headers,auto_context=False)
            self.assertEqual(code,200,data)

    def test_linked_staff_password_rotation_uses_grants_not_old_assignment(self):
        issued=self.issue('judge',station=3)
        aid=issued['credential']['id']
        self.grants(issued['credential'],[{'role':'registration'}])
        self.call('POST','/api/admin/workspace',{'mode':'rehearsal','confirm':'REHEARSAL'})
        for action in ('reset','revoke'):
            self.call('POST','/api/admin/credentials',{'action':action,'id':aid},status=409)
        self.call('POST','/api/admin/workspace',{'mode':'live','confirm':'LIVE'})
        rotated,_=self.call('POST','/api/admin/credentials',{'action':'reset','id':aid})
        self.assertEqual(rotated['credential']['id'],aid)
        cookie=self.login('judge',rotated['password'])
        self.assertEqual({d['role'] for d in self.call('GET','/api/access/me',cookie=cookie,dashboard='home')[0]['dashboards']},{'registration'})

    def test_rotation_cannot_reactivate_a_reassigned_station(self):
        old=self.issue('judge',station=3)
        self.call('POST','/api/admin/credentials',{'action':'revoke','id':old['credential']['id']})
        new=self.issue('registration')
        self.grants(new['credential'],[{'role':'registration'},{'role':'judge','station':3}])
        self.call('POST','/api/admin/credentials',{'action':'reset','id':old['credential']['id']},status=400)

    def test_rehearsal_reset_preserves_linked_live_participant_password(self):
        self.roster()
        live=self.issue('participant',pair=1,member=1)
        self.call('POST','/api/admin/workspace',{'mode':'rehearsal','confirm':'REHEARSAL'})
        practice,_=self.call('POST','/api/admin/credentials',{'action':'create','role':'participant','label':'Synthetic participant','pair':1,'member':1})
        self.call('POST','/api/admin/workspace',{'mode':'live','confirm':'LIVE'})
        self.call('POST','/api/admin/accounts',{'action':'link','id':practice['credential']['id'],'revision':1,
            'source_id':live['credential']['id'],'source_revision':1,'reason':'Same verified person across test datasets',
            'confirm':'LINK AND RETIRE SOURCE PASSWORD'})
        self.call('POST','/api/admin/workspace',{'mode':'rehearsal','confirm':'REHEARSAL'})
        S.WORKSPACE.reset('RESET REHEARSAL')
        self.call('POST','/api/admin/workspace',{'mode':'live','confirm':'LIVE'})
        me,_=self.call('POST','/api/access/login',{'password':practice['password']})
        self.assertEqual([d['role'] for d in me['dashboards']],['participant'])
