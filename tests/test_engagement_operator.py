"""Permission boundaries for the proposed event operator."""
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import engagement_catalog as catalog
import server
import test_multi_role as multi_role


class OperatorAuthority(unittest.TestCase):
    def test_operator_projection_excludes_drafts_private_questions_and_answer_keys(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title':'Published', 'kind':'quiz', 'questions':[
            {'type':'single','prompt':'Published prompt','options':['A','B'], 'correct':[1],
             'explanation':'Private explanation', 'evidence':'Private evidence'}]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        catalog.save_draft(state, {'title':'Unpublished secret', 'kind':'poll', 'questions':[
            {'type':'single','prompt':'Draft secret', 'options':['A','B']}]}, expected_revision=2)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=3)
        catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=4)
        catalog.submit_answer(state, run['id'], state['runs'][0]['delivery']['id'], 'private principal', 1)
        state['qa'].append({'id':'private', 'text':'Private question', 'status':'pending'})
        view = catalog.operator_view(state)
        data = str(view)
        for private in ('Unpublished secret', 'Draft secret', 'Private question', 'private principal',
                        'Private explanation', 'Private evidence', "'correct'"):
            self.assertNotIn(private, data)
        self.assertIn('Published prompt', data)
        self.assertEqual(view['runs'][0]['respondents'], 1)

    def test_operator_runs_published_activity_but_cannot_author_or_change_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            db = server.open_db(directory + '/event.db')
            self.addCleanup(db.close)
            service = SimpleNamespace(DB=db, LOCK=threading.RLock())
            organizer = {'role':'organizer', 'credential_id':'organizer'}
            operator = {'role':'operator', 'dashboard':'operator', 'credential_id':'operator'}
            payload = {'title':'Published poll', 'kind':'poll', 'questions':[
                {'type':'single', 'prompt':'Choose', 'options':['A','B']}]}
            draft = catalog.execute(service, 'save', payload, expected_revision=0,
                                    request_id='1'*32, identity=organizer)
            catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1,
                            request_id='2'*32, identity=organizer)
            for action, attempted in [('save', payload), ('publish', {'activity_id':draft['id']}),
                                      ('set_quiz_enabled', {'enabled':False}),
                                      ('configure_questions', {'participants':True,'guests':True})]:
                with self.subTest(action=action), self.assertRaises(PermissionError):
                    catalog.execute(service, action, attempted, expected_revision=2,
                                    request_id='3'*32, identity=operator)
            with self.assertRaises(catalog.Conflict):
                catalog.execute(service, 'open_run', {'activity_id':draft['id'],'version':2},
                                expected_revision=2, request_id='4'*32, identity=operator)
            run = catalog.execute(service, 'open_run', {'activity_id':draft['id'],'version':1},
                                  expected_revision=2, request_id='5'*32, identity=operator)
            opened = catalog.execute(service, 'open_question', {'run_id':run['id'],
                'question_id':run['snapshot']['questions'][0]['id'],'duration':None},
                expected_revision=3, request_id='6'*32, identity=operator)
            self.assertEqual(opened['delivery']['status'], 'open')

class OperatorHTTP(unittest.TestCase):
    setUp = multi_role.MultiRole.setUp
    request = multi_role.MultiRole.request
    login = multi_role.MultiRole.login
    issue = multi_role.MultiRole.issue
    call = multi_role.MultiRole.call
    profile = multi_role.MultiRole.profile

    def test_admin_can_preview_and_support_a_named_operator_without_extra_rights(self):
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            created, _ = self.call('POST', '/api/admin/credentials', {'action':'create-staff',
                'label':'Synthetic Operator', 'grants':[{'role':'operator'}]})
            credential = created['credential']['id']
            targets, _ = self.request('GET', '/api/admin/profiles', cookie=self.admin)
            self.assertTrue(any(t['role'] == 'operator' and t['credential_id'] == credential for t in targets['targets']))
            payload = {'title':'Approved poll','kind':'poll','questions':[{'type':'single',
                       'prompt':'Choose','options':['A','B']}]}
            state, _ = self.call('POST', '/api/desk/engagement', {'action':'save','payload':payload,
                'revision':0,'request_id':'b'*32})
            activity_id = state['activities'][0]['id']
            self.call('POST', '/api/desk/engagement', {'action':'publish','payload':{'activity_id':activity_id},
                'revision':1,'request_id':'c'*32})
            preview = self.profile('operator', credential_id=credential)
            self.request('GET', '/api/desk/engagement', cookie=self.admin, profile=preview)
            command = {'action':'save','payload':{},'revision':0,'request_id':'a'*32}
            self.request('POST', '/api/desk/engagement', command, self.admin, profile=preview, status=403)
            self.request('POST', '/api/desk/program', {}, self.admin, profile=preview, status=403)
            self.request('POST', '/api/access/impersonate/end', {}, self.admin, profile=preview)
            support = self.profile('operator', mode='support', credential_id=credential)
            self.request('POST', '/api/desk/engagement', command, self.admin, profile=support, status=403)
            self.request('POST', '/api/desk/program', {}, self.admin, profile=support, status=403)
            opened, _ = self.request('POST', '/api/desk/engagement', {'action':'open_run',
                'payload':{'activity_id':activity_id,'version':1},'revision':2,'request_id':'d'*32},
                self.admin, profile=support)
            self.assertEqual(opened['runs'][0]['status'], 'open')
            audit = multi_role.S.DB.execute("SELECT actor_credential,effective_credential,impersonation_mode FROM audit WHERE action='engagement-open_run'").fetchone()
            self.assertEqual(tuple(audit), ('owner-recovery', credential, 'support'))
            self.call('POST', '/api/admin/accounts', {'action':'roles', 'id':credential, 'revision':1,
                'reason':'Change operator coverage', 'grants':[{'role':'registration'}]})
            self.request('GET', '/api/desk/engagement', cookie=self.admin, profile=support, status=401)

    def test_operator_grant_cannot_be_added_when_feature_is_disabled(self):
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'0'}):
            self.call('POST', '/api/admin/credentials', {'action':'create-staff',
                'label':'Synthetic Operator', 'grants':[{'role':'operator'}]}, status=400)
            self.call('POST', '/api/admin/credentials', {'action':'create', 'role':'operator',
                'label':'Synthetic Operator'}, status=400)

    def test_granted_operator_has_only_restricted_engagement_authority(self):
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            created, _ = self.call('POST', '/api/admin/credentials', {'action':'create-staff',
                'label':'Synthetic Operator', 'grants':[{'role':'operator'}]})
            me, headers = self.call('POST', '/api/access/login', {'password':created['password']}, cookie='other-browser')
            self.assertEqual([item['role'] for item in me['dashboards']], ['operator'])
            cookie = next(item for item in headers['set-cookie'] if item.startswith('pp_access=')).split(';')[0]
            state, _ = self.call('GET', '/api/desk/engagement', cookie=cookie, dashboard='operator')
            self.assertEqual(state['activities'], [])
            self.call('POST', '/api/desk/engagement', {'action':'save','revision':0,
                'request_id':'1'*32,'payload':{}}, cookie=cookie, dashboard='operator', status=403)
            self.call('GET', '/api/desk/engagement/analytics.csv', cookie=cookie, dashboard='operator', status=403)
            self.call('GET', '/api/desk/engagement/legacy-preview', cookie=cookie, dashboard='operator', status=403)
            self.call('POST', '/api/desk/engagement-ai', {}, cookie=cookie, dashboard='operator', status=403)
            self.call('POST', '/api/admin/engagement-settings', {}, cookie=cookie, dashboard='operator', status=403)
            self.call('POST', '/api/desk/program', {}, cookie=cookie, dashboard='operator', status=403)
            payload = {'title':'Poll', 'kind':'poll', 'questions':[{'type':'single',
                       'prompt':'Question?', 'options':['A','B']}]}
            draft, _ = self.call('POST', '/api/desk/engagement', {'action':'save','revision':0,
                'request_id':'2'*32,'payload':payload})
            activity_id = draft['activities'][0]['id']
            self.call('POST', '/api/desk/engagement', {'action':'publish','revision':1,
                'request_id':'3'*32,'payload':{'activity_id':activity_id}})
            opened, _ = self.call('POST', '/api/desk/engagement', {'action':'open_run','revision':2,
                'request_id':'4'*32,'payload':{'activity_id':activity_id,'version':1}},
                cookie=cookie, dashboard='operator')
            self.assertEqual(opened['runs'][0]['status'], 'open')
            self.assertNotIn('qa', opened)
            self.assertNotIn('correct', str(opened))


if __name__ == '__main__':
    unittest.main()
