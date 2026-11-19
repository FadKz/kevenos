"""Audience questions remain private until an organizer moderates them."""
import unittest
from unittest.mock import patch
import engagement_catalog as catalog
import test_workspace_routes as workspace


class ModeratedQuestions(unittest.TestCase):
    def test_combined_question_stays_private_and_can_be_restored_through_http(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        participant = fixture.login(fixture.issue('participant', pair=1, member=1))
        guest = fixture.login(workspace.S.CFG['guest_password'])
        service = workspace.S
        operator = {'role':'organizer','credential_id':'operator'}
        catalog.execute(service, 'configure_questions', {'participants':True,'guests':True}, expected_revision=0,
                        request_id='1'*32, identity=operator)
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            path = '/api/engagement/questions'
            target = fixture.post(path, {'text':'Public question?', 'request_id':'2'*32}, cookie=participant)[0]['receipt']['id']
            source = fixture.post(path, {'text':'Private similar question?', 'request_id':'3'*32}, cookie=guest)[0]['receipt']['id']
            catalog.execute(service, 'moderate_question', {'id':target,'decision':'approve'}, expected_revision=1,
                            request_id='4'*32, identity=operator)
            catalog.execute(service, 'merge_question', {'id':source,'target_id':target}, expected_revision=2,
                            request_id='5'*32, identity=operator)
            guest_view = fixture.get(path, cookie=guest)[0]
            self.assertEqual(guest_view['own'][0]['status'], 'merged')
            self.assertEqual(guest_view['own'][0]['merged_into'], target)
            self.assertNotIn('Private similar question?', str(fixture.get(path, cookie=participant)[0]))
            catalog.execute(service, 'moderate_question', {'id':target,'decision':'reject'}, expected_revision=3,
                            request_id='6'*32, identity=operator)
            self.assertNotIn('merged_into', fixture.get(path, cookie=guest)[0]['own'][0])
            catalog.execute(service, 'moderate_question', {'id':source,'decision':'restore'}, expected_revision=4,
                            request_id='7'*32, identity=operator)
            self.assertEqual(fixture.get(path, cookie=guest)[0]['own'][0]['status'], 'pending')

    def test_organizer_can_combine_and_restore_pending_duplicate(self):
        state = catalog.empty()
        catalog.configure_questions(state, participants=True, guests=True, expected_revision=0)
        target = catalog.ask_question(state, 'participant:one', 'participant', 'Where is lunch?', 'a'*32)
        source = catalog.ask_question(state, 'guest:two', 'guest', 'Where do we eat?', 'b'*32)
        with self.assertRaises(catalog.Conflict):
            catalog.merge_question(state, source['id'], target['id'], expected_revision=1)
        catalog.moderate_question(state, target['id'], 'approve', expected_revision=1)
        catalog.merge_question(state, source['id'], target['id'], expected_revision=2)
        self.assertEqual(state['qa'][1]['status'], 'merged')
        self.assertEqual(state['qa'][1]['merged_into'], target['id'])
        self.assertEqual(catalog.ask_question(state, 'guest:two', 'guest', 'Where do we eat?', 'c'*32), source)
        self.assertEqual(len(state['qa']), 2)
        self.assertEqual([q['id'] for q in catalog.public_questions(state)], [target['id']])
        self.assertEqual(catalog.public_questions(state)[0]['votes'], 0)
        catalog.moderate_question(state, source['id'], 'restore', expected_revision=3)
        self.assertEqual(state['qa'][1]['status'], 'pending')
        self.assertNotIn('merged_into', state['qa'][1])

    def test_repeated_own_question_is_deduplicated_without_cross_user_disclosure(self):
        state = catalog.empty()
        catalog.configure_questions(state, participants=True, guests=True, expected_revision=0)
        first = catalog.ask_question(state, 'participant:one', 'participant', 'Same question?', 'a'*32)
        retry = catalog.ask_question(state, 'participant:one', 'participant', 'Same question?', 'b'*32)
        self.assertEqual(first, retry)
        self.assertEqual(len(state['qa']), 1)
        self.assertEqual(catalog.ask_question(state, 'participant:one', 'participant', 'Same question?', 'b'*32), first)
        with self.assertRaises(catalog.Conflict):
            catalog.ask_question(state, 'participant:one', 'participant', 'Different question?', 'b'*32)
        other = catalog.ask_question(state, 'guest:other', 'guest', 'Same question?', 'c'*32)
        self.assertNotEqual(other['id'], first['id'])
        self.assertEqual(catalog.public_questions(state), [])
        catalog.moderate_question(state, first['id'], 'reject', expected_revision=1)
        again = catalog.ask_question(state, 'participant:one', 'participant', 'Same question?', 'd'*32)
        self.assertNotEqual(again['id'], first['id'])
        self.assertNotIn('qa_request_aliases', catalog.desk_view(state))
        catalog.configure_questions(state, participants=False, guests=False, expected_revision=2)
        self.assertEqual(catalog.ask_question(state, 'participant:one', 'participant', 'Same question?', 'b'*32), first)

    def test_questions_require_enabled_audience_and_are_published_only_after_review(self):
        state = catalog.empty()
        with self.assertRaises(catalog.Conflict):
            catalog.ask_question(state, 'participant:one', 'participant', 'How does this work?', 'a'*32)
        catalog.configure_questions(state, participants=True, guests=False, expected_revision=0)
        with self.assertRaises(PermissionError):
            catalog.ask_question(state, 'guest:one', 'guest', 'Can I ask?', 'b'*32)
        receipt = catalog.ask_question(state, 'participant:one', 'participant', 'How does this work?', 'a'*32)
        self.assertEqual(receipt, catalog.ask_question(state, 'participant:one', 'participant', 'How does this work?', 'a'*32))
        with self.assertRaises(catalog.Conflict):
            catalog.ask_question(state, 'participant:one', 'participant', 'Different question', 'a'*32)
        self.assertEqual(catalog.public_questions(state), [])
        catalog.moderate_question(state, receipt['id'], 'approve', expected_revision=1)
        public = catalog.public_questions(state)
        self.assertEqual(public[0]['text'], 'How does this work?')
        self.assertNotIn('participant:one', str(public))
        catalog.moderate_question(state, receipt['id'], 'answer', answer='Here is how.', expected_revision=2)
        self.assertEqual(catalog.public_questions(state)[0]['answer'], 'Here is how.')
        catalog.moderate_question(state, receipt['id'], 'reject', expected_revision=3)
        self.assertEqual(catalog.public_questions(state), [])
        catalog.configure_questions(state, participants=False, guests=False, expected_revision=4)
        self.assertEqual(receipt, catalog.ask_question(state, 'participant:one', 'participant', 'How does this work?', 'a'*32))

    def test_question_input_and_rate_limits(self):
        state = catalog.empty()
        catalog.configure_questions(state, participants=True, guests=True, expected_revision=0)
        with self.assertRaises(ValueError):
            catalog.ask_question(state, 'guest:one', 'guest', ' ', 'a'*32)
        for index in range(5):
            catalog.ask_question(state, 'guest:one', 'guest', f'Question {index}?', f'{index:032x}')
        with self.assertRaises(catalog.Conflict):
            catalog.ask_question(state, 'guest:one', 'guest', 'One more?', 'f'*32)
        self.assertEqual(state['revision'], 1, 'attendee submissions must not invalidate organizer drafts')

    def test_only_approved_questions_can_receive_idempotent_votes(self):
        state = catalog.empty()
        catalog.configure_questions(state, participants=True, guests=True, expected_revision=0)
        receipt = catalog.ask_question(state, 'participant:one', 'participant', 'Question?', 'a'*32)
        with self.assertRaises(catalog.Conflict):
            catalog.vote_question(state, receipt['id'], 'guest:one', 'guest', True)
        catalog.moderate_question(state, receipt['id'], 'approve', expected_revision=1)
        catalog.vote_question(state, receipt['id'], 'guest:one', 'guest', True)
        catalog.vote_question(state, receipt['id'], 'guest:one', 'guest', True)
        catalog.vote_question(state, receipt['id'], 'participant:one', 'participant', True)
        public = catalog.public_questions(state, principal='guest:one')
        self.assertEqual(public[0]['votes'], 2)
        self.assertTrue(public[0]['upvoted'])
        self.assertNotIn('participant:one', str(public))
        catalog.vote_question(state, receipt['id'], 'guest:one', 'guest', False)
        self.assertEqual(catalog.public_questions(state)[0]['votes'], 1)
        self.assertEqual(state['revision'], 2)

    def test_participant_and_guest_question_api_is_moderated_and_session_scoped(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        participant = fixture.login(fixture.issue('participant', pair=1, member=1))
        guest = fixture.login(workspace.S.CFG['guest_password'])
        service = workspace.S
        catalog.execute(service, 'configure_questions', {'participants':True, 'guests':True}, expected_revision=0,
                        request_id='1'*32, identity={'role':'organizer', 'credential_id':'operator'})
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            path = '/api/engagement/questions'
            result, _ = fixture.post(path, {'text':'Participant question?', 'request_id':'2'*32}, cookie=participant)
            question_id = result['receipt']['id']
            repeated, _ = fixture.post(path, {'text':'Participant question?', 'request_id':'5'*32}, cookie=participant)
            self.assertEqual(repeated['receipt']['id'], question_id)
            result, _ = fixture.get(path, cookie=guest)
            self.assertEqual(result['questions'], [])
            self.assertEqual(result['own'], [])
            result, _ = fixture.get(path, cookie=participant)
            self.assertEqual(len(result['own']), 1)
            self.assertEqual(result['own'][0]['text'], 'Participant question?')
            self.assertEqual(result['own'][0]['status'], 'pending')
            catalog.execute(service, 'moderate_question', {'id':question_id, 'decision':'approve'}, expected_revision=1,
                            request_id='3'*32, identity={'role':'organizer', 'credential_id':'operator'})
            result, _ = fixture.get(path, cookie=guest)
            self.assertEqual(result['questions'][0]['text'], 'Participant question?')
            fixture.post(path, {'question_id':question_id, 'upvote':True}, cookie=guest)
            fixture.post(path, {'question_id':question_id, 'upvote':True}, cookie=guest)
            voted = fixture.get(path, cookie=guest)[0]['questions'][0]
            self.assertEqual(voted['votes'], 1)
            self.assertTrue(voted['upvoted'])
            self.assertNotIn('guest:', str(voted))
            fixture.post(path, {'text':'Guest question?', 'request_id':'4'*32}, cookie=guest)
            second_guest = fixture.login(workspace.S.CFG['guest_password'])
            self.assertEqual(fixture.get(path, cookie=second_guest)[0]['own'], [])
            self.assertEqual(len(fixture.get(path, cookie=participant)[0]['questions']), 1)
            fixture.get(path, cookie=fixture.admin, status=403)
