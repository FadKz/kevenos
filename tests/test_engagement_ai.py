"""AI drafting never publishes content and requires explicit source disclosure."""
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import engagement_ai
import test_workspace_routes as workspace


class Drafting(unittest.TestCase):
    def test_http_draft_requires_organizer_and_does_not_save_or_publish(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        participant = fixture.login(fixture.issue('participant', pair=1, member=1))
        staff = fixture.login(fixture.issue('desk'))
        body = {'source':'Example source', 'count':1, 'kind':'poll', 'disclose':True}
        result = {'title':'Suggested poll','kind':'poll','review_required':True,'questions':[]}
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1', 'KEVENOS_ENGAGEMENT_AI_ENABLED':'1'}), \
             patch.object(engagement_ai, 'draft', return_value=result):
            path = '/api/desk/engagement-ai'
            fixture.post(path, body, cookie=participant, status=403)
            fixture.post(path, body, cookie=staff, headers={'X-Dashboard':'scoring'}, status=403)
            value, _ = fixture.post(path, body, cookie=fixture.admin)
            self.assertTrue(value['review_required'])
            self.assertIsNone(workspace.S.kv_get('engagement_catalog_v1'))

    def test_generated_questions_require_verbatim_source_evidence_and_review(self):
        source = 'The event begins with a welcome. Everyone joins a short practice session.'
        model = {'title':'Practice', 'kind':'quiz', 'questions':[{'type':'single',
                 'prompt':'What happens first?', 'options':['Welcome','Awards'], 'correct':[0],
                 'evidence':'The event begins with a welcome.'}]}
        with patch.object(engagement_ai, '_generate', return_value=json.dumps(model)) as generate:
            result = engagement_ai.draft(source, count=1, kind='quiz', disclose=True)
        self.assertEqual(result['questions'][0]['evidence'], 'The event begins with a welcome.')
        self.assertTrue(result['review_required'])
        generate.assert_called_once()
        with patch.object(engagement_ai, '_generate') as generate:
            with self.assertRaises(ValueError):
                engagement_ai.draft(source, count=1, kind='quiz', disclose=False)
            generate.assert_not_called()

    def test_model_output_is_untrusted_and_cannot_invent_evidence(self):
        model = {'title':'Poll','kind':'poll','questions':[{'type':'single','prompt':'Choose?',
                 'options':['A','B'],'correct':[],'evidence':'invented fact'}]}
        with patch.object(engagement_ai, '_generate', return_value=json.dumps(model)):
            with self.assertRaises(ValueError):
                engagement_ai.draft('Original source.', count=1, kind='poll', disclose=True)
        with patch.object(engagement_ai, '_generate', return_value='not json'):
            with self.assertRaises(ValueError):
                engagement_ai.draft('Original source.', count=1, kind='poll', disclose=True)

    def test_gateway_client_is_bounded_and_requires_explicit_configuration(self):
        captured = {}
        class Client:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            def create(self, **kwargs):
                captured['request'] = kwargs
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
            def close(self): captured['closed'] = True
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_AI_ENABLED':'1',
                'TM_LLM_GATEWAY_URL':'https://gateway.example.test/v1', 'TM_LLM_GATEWAY_KEY':'synthetic-only'}), \
             patch('openai.OpenAI', Client):
            self.assertEqual(engagement_ai._generate('Synthetic source', 1, 'quiz'), '{}')
        self.assertEqual(captured['base_url'], 'https://gateway.example.test/v1')
        self.assertEqual((captured['timeout'], captured['max_retries']), (30.0, 0))
        self.assertEqual(captured['request']['model'], 'tier/mid')
        self.assertTrue(captured['closed'])

    def test_gateway_failure_is_redacted_and_manual_operations_remain_available(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1', 'KEVENOS_ENGAGEMENT_AI_ENABLED':'1'}), \
             patch.object(engagement_ai, '_generate', side_effect=RuntimeError('sensitive provider detail')):
            result, _ = fixture.post('/api/desk/engagement-ai', {'source':'Example', 'count':1, 'kind':'poll', 'disclose':True},
                                     cookie=fixture.admin, status=503)
            self.assertNotIn('sensitive provider detail', str(result))
            value, _ = fixture.get('/api/desk/engagement', cookie=fixture.admin)
            self.assertEqual(value['revision'], 0)

    def test_revoked_session_cannot_receive_delayed_ai_output(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        def revoke(*args, **kwargs):
            with service.AUTH_DB:
                service.AUTH_DB.execute('DELETE FROM role_sessions')
            return {'title':'Private suggestion','review_required':True}
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1', 'KEVENOS_ENGAGEMENT_AI_ENABLED':'1'}), \
             patch.object(engagement_ai, 'draft', side_effect=revoke):
            value, _ = fixture.post('/api/desk/engagement-ai', {'source':'Example','count':1,'kind':'poll','disclose':True},
                                    cookie=fixture.admin, status=401)
            self.assertNotIn('Private suggestion', str(value))
