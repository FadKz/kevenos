"""The compatibility inventory reports counts without rewriting old event records."""
import json
import time
import unittest
from unittest.mock import patch
import engagement_catalog as catalog
import test_workspace_routes as workspace


class LegacyInventory(unittest.TestCase):
    def test_legacy_delivery_guard_distinguishes_open_expired_and_bad_deadlines(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        db = workspace.S.DB
        with db: catalog._write(db, 'event_quiz_v1', {'runs':[{'status':'open','end':time.time()+60}]})
        self.assertTrue(catalog._legacy_delivery_open(db))
        with db: catalog._write(db, 'event_quiz_v1', {'runs':[{'status':'open','end':time.time()-60}]})
        self.assertFalse(catalog._legacy_delivery_open(db))
        with db: catalog._write(db, 'audience_questions_v1', {'packs':[{'status':'live','closesAt':None}]})
        self.assertTrue(catalog._legacy_delivery_open(db))
        with db: catalog._write(db, 'audience_questions_v1', {'packs':[{'status':'live','closesAt':'invalid'}]})
        with self.assertRaises(catalog.Conflict):
            catalog._legacy_delivery_open(db)

    def test_malformed_legacy_state_blocks_new_question_without_changing_run(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        service = workspace.S
        operator = {'role':'organizer','credential_id':'operator'}
        draft = catalog.execute(service, 'save', {'title':'Poll','kind':'poll','questions':[
            {'type':'single','prompt':'Question?','options':['A','B']}]}, expected_revision=0,
            request_id='1'*32, identity=operator)
        catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1,
                        request_id='2'*32, identity=operator)
        run = catalog.execute(service, 'open_run', {'activity_id':draft['id'],'version':1}, expected_revision=2,
                              request_id='3'*32, identity=operator)
        with service.DB: catalog._write(service.DB, 'event_quiz_v1', ['unexpected'])
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}), self.assertRaises(catalog.Conflict):
            catalog.execute(service, 'open_question', {'run_id':run['id'],
                'question_id':run['snapshot']['questions'][0]['id']}, expected_revision=3,
                request_id='4'*32, identity=operator)
        self.assertEqual(service.kv_get(catalog.KEY)['revision'], 3)
        self.assertNotIn('delivery', service.kv_get(catalog.KEY)['runs'][0])
        with service.DB: catalog._write(service.DB, 'event_quiz_v1', {'runs':[{'status':'open','end':time.time()+60}]})
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}), self.assertRaises(catalog.Conflict):
            catalog.execute(service, 'open_question', {'run_id':run['id'],
                'question_id':run['snapshot']['questions'][0]['id']}, expected_revision=3,
                request_id='5'*32, identity=operator)
        self.assertEqual(service.kv_get(catalog.KEY)['revision'], 3)

    def test_inventory_reports_malformed_legacy_records_without_writes(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        service = workspace.S
        bad = {'rev':1,'runs':[],'answers':{'synthetic':'not a mapping'}}
        with service.DB: catalog._write(service.DB, 'event_quiz_v1', bad)
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            result, _ = fixture.get('/api/desk/engagement/legacy-preview', cookie=fixture.admin, status=409)
        self.assertIn('require inspection', result['error'])
        self.assertEqual(service.kv_get('event_quiz_v1'), bad)
        with service.DB: catalog._write(service.DB, 'event_quiz_v1', ['unexpected'])
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            result, _ = fixture.get('/api/desk/engagement/legacy-preview', cookie=fixture.admin, status=409)
        self.assertIn('require inspection', result['error'])

    def test_inventory_counts_legacy_cases_without_disclosing_answers(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        service = workspace.S
        quiz = {'rev':2, 'runs':[
            {'id':'r1','status':'closed','kind':'warmup','points':0},
            {'id':'r2','status':'void','kind':'live','points':2,'excluded':[3], 'prediction':True},
            {'id':'r3','status':'open','kind':'waiting','points':1}],
            'answers':{'private-person':{'r1':{'key':'private-answer'},'r2':{'key':'A'}}}}
        with service.DB:
            catalog._write(service.DB, 'event_quiz_v1', quiz)
        result = catalog.legacy_preview(service.DB)
        self.assertEqual(result['quiz']['answer_entries'], 2)
        self.assertEqual(result['quiz']['cases'], {'voided':1,'unscored':1,'excluded_audiences':1,
            'predictions':1,'warmup_or_post':1,'catchup_or_waiting':1,'open':1})
        self.assertNotIn('private-person', json.dumps(result))
        self.assertNotIn('private-answer', json.dumps(result))

    def test_copy_legacy_draft_is_reviewed_idempotent_and_preserves_original(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        pack_id = 'a'*32
        old = {'rev':3,'packs':[{'id':pack_id,'status':'draft','title':'Original poll','description':'Context',
            'questions':[{'id':'b'*32,'type':'choice','prompt':'Choose?', 'options':[{'key':'A','text':'First'},{'key':'B','text':'Second'}]},
                         {'id':'c'*32,'type':'rating','prompt':'How useful?'}]}]}
        with service.DB:
            catalog._write(service.DB, 'audience_questions_v1', old)
        preview = catalog.legacy_preview(service.DB)
        item = preview['audience']['drafts'][0]
        self.assertEqual(item['id'], pack_id)
        operator = {'role':'organizer','credential_id':'operator'}
        payload = {'pack_id':pack_id,'source_digest':item['digest']}
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            copied = catalog.execute(service, 'copy_legacy_pack', payload, expected_revision=0, request_id='1'*32, identity=operator)
            self.assertEqual(copied['title'], 'Original poll')
            self.assertEqual(copied['description'], 'Context')
            self.assertEqual([q['type'] for q in copied['questions']], ['single','rating'])
            self.assertEqual(catalog.execute(service, 'copy_legacy_pack', payload, expected_revision=0, request_id='1'*32, identity=operator), copied)
            with self.assertRaises(catalog.Conflict):
                catalog.execute(service, 'copy_legacy_pack', payload, expected_revision=1, request_id='2'*32, identity=operator)
            self.assertEqual(service.kv_get('audience_questions_v1'), old)
            self.assertEqual(service.kv_get(catalog.KEY)['legacy_imports'][pack_id]['activity_id'], copied['id'])

    def test_copy_rejects_changed_or_non_draft_source_without_writes(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        pack_id = 'a'*32
        old = {'rev':1,'packs':[{'id':pack_id,'status':'draft','title':'Original','description':'',
            'questions':[{'id':'b'*32,'type':'text','prompt':'Question?'}]}]}
        with service.DB: catalog._write(service.DB, 'audience_questions_v1', old)
        digest = catalog.legacy_preview(service.DB)['audience']['drafts'][0]['digest']
        old['packs'][0]['title'] = 'Changed'
        with service.DB: catalog._write(service.DB, 'audience_questions_v1', old)
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}), self.assertRaises(catalog.Conflict):
            catalog.execute(service, 'copy_legacy_pack', {'pack_id':pack_id,'source_digest':digest}, expected_revision=0,
                            request_id='3'*32, identity={'role':'organizer','credential_id':'operator'})
        self.assertIsNone(service.kv_get(catalog.KEY))

    def test_legacy_preview_is_read_only_and_does_not_disclose_answer_contents(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        quiz = {'rev':2,'runs':[{'id':'r1','status':'closed'}], 'answers':{'person':{'r1':{'key':'secret-answer'}}}}
        audience = {'rev':3,'packs':[{'id':'draft','status':'draft','title':'Private draft'},
                                     {'id':'sent','status':'closed','title':'Past question'}]}
        with service.DB:
            catalog._write(service.DB, 'event_quiz_v1', quiz)
            catalog._write(service.DB, 'audience_questions_v1', audience)
            catalog._write(service.DB, 'audience_answers_v1:synthetic', {'secret':'private response'})
        before = list(service.DB.execute('SELECT k,v FROM kv ORDER BY k'))
        preview = catalog.legacy_preview(service.DB)
        self.assertEqual(preview['quiz']['runs'], 1)
        self.assertEqual(preview['quiz']['answering_accounts'], 1)
        self.assertEqual(preview['audience']['draft_packs'], 1)
        self.assertEqual(preview['audience']['answer_records'], 1)
        self.assertNotIn('secret-answer', json.dumps(preview))
        self.assertNotIn('private response', json.dumps(preview))
        self.assertEqual(before, list(service.DB.execute('SELECT k,v FROM kv ORDER BY k')))
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            value, _ = fixture.get('/api/desk/engagement/legacy-preview', cookie=fixture.admin)
            self.assertEqual(value, preview)
