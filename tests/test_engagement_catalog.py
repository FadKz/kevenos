"""Versioned event-owned engagement drafts and immutable run snapshots."""
import unittest
from unittest.mock import patch
import json
import os
import sqlite3
import threading
from types import SimpleNamespace
import engagement_catalog as catalog
import test_workspace_routes as workspace


class EngagementCatalogTests(unittest.TestCase):
    def test_storage_is_transactional_and_rejects_stale_writes(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)')
        service = SimpleNamespace(DB=db, LOCK=threading.RLock())
        payload = {'title': 'Example', 'kind': 'poll', 'questions': [
            {'prompt': 'Choose', 'type': 'single', 'options': ['A', 'B']}
        ]}
        catalog.persist(service, 'save', payload, expected_revision=0)
        saved = json.loads(db.execute('SELECT v FROM kv WHERE k=?', (catalog.KEY,)).fetchone()[0])
        self.assertEqual(saved['revision'], 1)
        with self.assertRaises(catalog.Conflict):
            catalog.persist(service, 'save', payload, expected_revision=0)
        unchanged = json.loads(db.execute('SELECT v FROM kv WHERE k=?', (catalog.KEY,)).fetchone()[0])
        self.assertEqual(saved, unchanged)

    def test_edit_preserves_existing_question_ids_and_rejects_foreign_ids(self):
        state = catalog.empty()
        original = catalog.save_draft(state, {'title': 'Poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}
        ]}, expected_revision=0)
        question = original['questions'][0]
        updated = catalog.save_draft(state, {'id': original['id'], 'title': 'Poll', 'kind': 'poll', 'questions': [
            {**question, 'prompt': 'Choose again'}
        ]}, expected_revision=1)
        self.assertEqual(updated['questions'][0]['id'], question['id'])
        with self.assertRaises(ValueError):
            catalog.save_draft(state, {'id': original['id'], 'title': 'Poll', 'kind': 'poll', 'questions': [
                {**question, 'id': 'foreign'}
            ]}, expected_revision=2)
        self.assertEqual(state['revision'], 2)

    def test_omitted_audience_on_edit_keeps_existing_selection(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title':'Poll','kind':'poll','audiences':['guest'], 'questions':[
            {'type':'single','prompt':'Question?','options':['A','B']}]}, expected_revision=0)
        edited = catalog.save_draft(state, {'id':draft['id'], 'title':'Revised','kind':'poll',
            'questions':draft['questions']}, expected_revision=1)
        self.assertEqual(edited['audiences'], ['guest'])

    def test_published_version_and_run_are_immutable_when_draft_changes(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Session quiz', 'kind': 'quiz', 'questions': [
            {'prompt': 'What is two plus two?', 'type': 'single', 'options': ['3', '4'], 'correct': [1]},
            {'prompt': 'Future question', 'type': 'single', 'options': ['A', 'B'], 'correct': [0]}
        ]}, expected_revision=0)
        version = catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], version['version'], expected_revision=2)
        question_id = run['snapshot']['questions'][0]['id']
        self.assertEqual(catalog.player_view(run)['questions'], [])
        run = catalog.open_question(state, run['id'], question_id, expected_revision=3)
        changed = catalog.save_draft(state, {'id': draft['id'], 'title': 'Revised', 'kind': 'quiz', 'questions': [
            {'prompt': 'What is three plus three?', 'type': 'single', 'options': ['5', '6'], 'correct': [1]}
        ]}, expected_revision=4)
        self.assertEqual(changed['title'], 'Revised')
        self.assertEqual(run['snapshot']['title'], 'Session quiz')
        self.assertEqual(run['snapshot']['questions'][0]['prompt'], 'What is two plus two?')
        player = catalog.player_view(run)
        self.assertEqual(player['id'], run['id'])
        self.assertEqual(player['activity_id'], draft['id'])
        self.assertEqual(len(player['questions']), 1)
        self.assertNotIn('Future question', str(player))
        self.assertNotIn('correct', player['questions'][0])

    def test_conflict_and_invalid_quiz_do_not_change_catalog(self):
        state = catalog.empty()
        with self.assertRaises(catalog.Conflict):
            catalog.save_draft(state, {'title': 'Test', 'kind': 'quiz', 'questions': []}, expected_revision=1)
        with self.assertRaises(ValueError):
            catalog.save_draft(state, {'title': 'Test', 'kind': 'quiz', 'questions': [
                {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B'], 'correct': [7]}
            ]}, expected_revision=0)
        self.assertEqual(state, catalog.empty())

    def test_command_rejects_unrecognized_payload_fields(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)')
        service = SimpleNamespace(DB=db, LOCK=threading.RLock())
        with self.assertRaises(ValueError) as error:
            catalog.persist(service, 'open_run', {'activity_id':'x', 'version':1, 'unexpected':True}, expected_revision=0)
        self.assertIs(type(error.exception), ValueError)
        self.assertIsNone(db.execute('SELECT * FROM kv').fetchone())

    def test_timed_question_lifecycle_hides_answer_until_explicit_reveal(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'First?', 'options': ['A', 'B'], 'correct': [1]},
            {'type': 'single', 'prompt': 'Later?', 'options': ['C', 'D'], 'correct': [0]},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        first, second = [q['id'] for q in run['snapshot']['questions']]
        with patch.object(catalog.time, 'time', return_value=1000):
            run = catalog.open_question(state, run['id'], first, expected_revision=3, duration=15)
        self.assertEqual(run['delivery']['ends_at'], 1015)
        self.assertNotIn('correct', str(catalog.player_view(run)))
        with self.assertRaises(catalog.Conflict):
            catalog.open_question(state, run['id'], second, expected_revision=4)
        run = catalog.close_question(state, run['id'], expected_revision=4)
        self.assertNotIn('correct', str(catalog.player_view(run)))
        run = catalog.reveal_question(state, run['id'], expected_revision=5)
        self.assertEqual(catalog.player_view(run)['questions'][0]['correct'], [1])
        with self.assertRaises(catalog.Conflict):
            catalog.open_question(state, run['id'], first, expected_revision=6)
        run = catalog.open_question(state, run['id'], second, expected_revision=6, duration=None)
        self.assertIsNone(run['delivery']['ends_at'])
        self.assertNotIn('First?', str(catalog.player_view(run)))

    def test_question_duration_is_bounded_and_bool_version_is_rejected(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B']},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        with self.assertRaises(catalog.Conflict):
            catalog.open_run(state, draft['id'], True, expected_revision=2)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        for invalid in (True, 0, -1, 3601, '15'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'],
                                      expected_revision=3, duration=invalid)
        self.assertEqual(state['revision'], 3)

    def test_answer_is_accepted_once_and_retry_survives_expiry(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B'], 'correct': [1]},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        with patch.object(catalog.time, 'time', return_value=100):
            run = catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'],
                                        expected_revision=3, duration=15)
        delivery = run['delivery']['id']
        with patch.object(catalog.time, 'time', return_value=114):
            receipt = catalog.submit_answer(state, run['id'], delivery, 'person-1', 1)
        self.assertEqual(state['revision'], 4, 'answers must not invalidate the organizer controls')
        with patch.object(catalog.time, 'time', return_value=116):
            self.assertEqual(receipt, catalog.submit_answer(state, run['id'], delivery, 'person-1', 1))
            with self.assertRaises(catalog.Conflict):
                catalog.submit_answer(state, run['id'], delivery, 'person-2', 0)
        with self.assertRaises(catalog.Conflict):
            catalog.submit_answer(state, run['id'], delivery, 'person-1', 0)
        view = catalog.player_view(state['runs'][0])
        self.assertNotIn('person-1', str(view))
        self.assertNotIn('correct', str(view))

    def test_answer_rejects_foreign_delivery_and_invalid_values(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B']},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        run = catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=3)
        for invalid in (True, -1, 2, '1'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                catalog.submit_answer(state, run['id'], run['delivery']['id'], 'person-1', invalid)
        with self.assertRaises(catalog.Conflict):
            catalog.submit_answer(state, run['id'], 'foreign', 'person-1', 0)

    def test_multiple_choice_and_unscored_feedback_question_types(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'multi', 'prompt': 'Select both', 'options': ['A', 'B', 'C'], 'correct': [0, 2]},
            {'type': 'true_false', 'prompt': 'Is it true?', 'correct': [0]},
        ]}, expected_revision=0)
        self.assertEqual(draft['questions'][1]['options'], ['True', 'False'])
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        run = catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=3)
        with self.assertRaises(ValueError):
            catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p1', [0, 0])
        receipt = catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p1', [2, 0])
        self.assertEqual(receipt, catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p1', [0, 2]))
        feedback = catalog.save_draft(state, {'title': 'Feedback', 'kind': 'poll', 'questions': [
            {'type': 'rating', 'prompt': 'How useful?'},
            {'type': 'text', 'prompt': 'What would you change?'},
        ]}, expected_revision=4)
        self.assertEqual([q['type'] for q in feedback['questions']], ['rating', 'text'])

    def test_only_one_question_can_be_open_for_the_event(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B']},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        first = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        second = catalog.open_run(state, draft['id'], 1, expected_revision=3)
        catalog.open_question(state, first['id'], first['snapshot']['questions'][0]['id'], expected_revision=4)
        with self.assertRaises(catalog.Conflict):
            catalog.open_question(state, second['id'], second['snapshot']['questions'][0]['id'], expected_revision=5)

    def test_player_only_sees_their_score_after_reveal(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B'], 'correct': [1]},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        run = catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=3)
        catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p1', 1)
        catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p2', 0)
        self.assertNotIn('result', catalog.player_view(state['runs'][0], principal='p1'))
        catalog.close_question(state, run['id'], expected_revision=4)
        self.assertNotIn('result', catalog.player_view(state['runs'][0], principal='p1'))
        catalog.reveal_question(state, run['id'], expected_revision=5)
        self.assertEqual(catalog.player_view(state['runs'][0], principal='p1')['result'], {'earned': 1, 'available': 1})
        self.assertEqual(catalog.player_view(state['runs'][0], principal='p2')['result'], {'earned': 0, 'available': 1})
        self.assertNotIn('result', catalog.player_view(state['runs'][0], principal='p3'))

    def test_resend_keeps_first_answer_and_late_retry_does_not_double_score(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B'], 'correct': [1]},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        run = catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=3)
        original = run['delivery']['id']
        receipt = catalog.submit_answer(state, run['id'], original, 'p1', 1)
        catalog.close_question(state, run['id'], expected_revision=4)
        run = catalog.resend_question(state, run['id'], expected_revision=5, duration=30)
        self.assertNotEqual(run['delivery']['id'], original)
        self.assertEqual(receipt, catalog.submit_answer(state, run['id'], original, 'p1', 1))
        with self.assertRaises(catalog.Conflict):
            catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p1', 0)
        catalog.submit_answer(state, run['id'], run['delivery']['id'], 'p2', 0)
        catalog.close_question(state, run['id'], expected_revision=6)
        catalog.reveal_question(state, run['id'], expected_revision=7)
        self.assertEqual(catalog.player_view(state['runs'][0], principal='p1')['result'], {'earned':1, 'available':1})
        self.assertEqual(catalog.player_view(state['runs'][0], principal='p2')['result'], {'earned':0, 'available':1})

    def test_organizer_analytics_suppresses_small_breakdowns_and_individual_answers(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B']},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        run = catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=3)
        for i in range(4):
            catalog.submit_answer(state, run['id'], run['delivery']['id'], f'private-person-{i}', i % 2)
        projection = catalog.desk_view(state)
        self.assertNotIn('private-person', str(projection))
        self.assertNotIn('responses', projection['runs'][0])
        self.assertEqual(projection['runs'][0]['analytics']['respondents'], 4)
        self.assertIsNone(projection['runs'][0]['analytics']['distribution'])
        catalog.submit_answer(state, run['id'], run['delivery']['id'], 'private-person-4', 0)
        analytics = catalog.desk_view(state)['runs'][0]['analytics']
        self.assertEqual(analytics['distribution'], [3, 2])
        self.assertEqual(analytics['denominator'], 5)

    def test_hall_requires_explicit_sharing_and_hides_answer_until_reveal(self):
        state = catalog.empty()
        draft = catalog.save_draft(state, {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Current question', 'options': ['A', 'B'], 'correct': [1]},
            {'type': 'single', 'prompt': 'Future secret', 'options': ['C', 'D'], 'correct': [0]},
        ]}, expected_revision=0)
        catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], 1, expected_revision=2)
        catalog.open_question(state, run['id'], run['snapshot']['questions'][0]['id'], expected_revision=3)
        self.assertIsNone(catalog.hall_view(state))
        catalog.share_hall(state, run['id'], True, expected_revision=4)
        visible = catalog.hall_view(state)
        self.assertEqual(visible['prompt'], 'Current question')
        self.assertNotIn('correct', str(visible))
        self.assertNotIn('Future secret', str(visible))
        self.assertNotIn('options', visible)
        catalog.close_question(state, run['id'], expected_revision=5)
        self.assertNotIn('correct', str(catalog.hall_view(state)))
        catalog.reveal_question(state, run['id'], expected_revision=6)
        self.assertEqual(catalog.hall_view(state)['correct'], [1])
        catalog.share_hall(state, run['id'], False, expected_revision=7)
        self.assertIsNone(catalog.hall_view(state))


class WorkspaceIsolationTests(unittest.TestCase):
    def test_guest_cannot_read_or_answer_participant_only_activity(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        guest = fixture.login(workspace.S.CFG['guest_password'])
        service = workspace.S
        operator = {'role':'organizer','credential_id':'operator'}
        draft = catalog.execute(service, 'save', {'title':'Private audience','kind':'poll','questions':[
            {'type':'single','prompt':'Participants only','options':['A','B']}]}, expected_revision=0, request_id='1'*32, identity=operator)
        catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1, request_id='2'*32, identity=operator)
        run = catalog.execute(service, 'open_run', {'activity_id':draft['id'],'version':1}, expected_revision=2, request_id='3'*32, identity=operator)
        run = catalog.execute(service, 'open_question', {'run_id':run['id'],'question_id':run['snapshot']['questions'][0]['id']},
                              expected_revision=3, request_id='4'*32, identity=operator)
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2':'1'}):
            path = '/api/engagement/player'
            self.assertIsNone(fixture.get(path, cookie=guest)[0]['question'])
            fixture.post(path, {'run_id':run['id'], 'delivery_id':run['delivery']['id'], 'choice':0}, cookie=guest, status=403)

    def test_guest_can_only_join_an_explicitly_guest_enabled_activity(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        guest = fixture.login(workspace.S.CFG['guest_password'])
        service = workspace.S
        operator = {'role':'organizer', 'credential_id':'operator'}
        draft = catalog.execute(service, 'save', {'title':'Guest poll', 'kind':'poll', 'audiences':['participant','guest'],
            'questions':[{'type':'single','prompt':'Choose','options':['A','B']}]}, expected_revision=0, request_id='1'*32, identity=operator)
        catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1, request_id='2'*32, identity=operator)
        run = catalog.execute(service, 'open_run', {'activity_id':draft['id'], 'version':1}, expected_revision=2, request_id='3'*32, identity=operator)
        run = catalog.execute(service, 'open_question', {'run_id':run['id'], 'question_id':run['snapshot']['questions'][0]['id']},
                              expected_revision=3, request_id='4'*32, identity=operator)
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2':'1'}):
            path = '/api/engagement/player'
            view, _ = fixture.get(path, cookie=guest)
            self.assertEqual(view['question']['questions'][0]['prompt'], 'Choose')
            fixture.post(path, {'run_id':run['id'], 'delivery_id':run['delivery']['id'], 'choice':0}, cookie=guest)
            self.assertTrue(fixture.get(path, cookie=guest)[0]['answered'])
            guest2 = fixture.login(service.CFG['guest_password'])
            self.assertFalse(fixture.get(path, cookie=guest2)[0]['answered'])
            self.assertNotIn('guest:', str(fixture.get(path, cookie=guest)[0]))

    def test_hall_http_projection_follows_explicit_sharing_without_early_answer(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        identity = {'role': 'organizer', 'credential_id': 'operator'}
        draft = catalog.execute(service, 'save', {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Visible prompt?', 'options': ['A', 'B'], 'correct': [1]},
        ]}, expected_revision=0, request_id='1'*32, identity=identity)
        catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1, request_id='2'*32, identity=identity)
        run = catalog.execute(service, 'open_run', {'activity_id':draft['id'], 'version':1}, expected_revision=2, request_id='3'*32, identity=identity)
        catalog.execute(service, 'open_question', {'run_id':run['id'], 'question_id':run['snapshot']['questions'][0]['id']},
                        expected_revision=3, request_id='4'*32, identity=identity)
        catalog.execute(service, 'share_hall', {'run_id':run['id'], 'share':True}, expected_revision=4, request_id='5'*32, identity=identity)
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2':'1'}):
            result = service.public_state(names=False, presenter=True)
            self.assertEqual(result['quiz']['prompt'], 'Visible prompt?')
            self.assertEqual(result['quiz']['options'], [])
            self.assertNotIn('correct', result['quiz'])
            catalog.execute(service, 'close_question', {'run_id':run['id']}, expected_revision=5, request_id='6'*32, identity=identity)
            catalog.execute(service, 'reveal_question', {'run_id':run['id']}, expected_revision=6, request_id='7'*32, identity=identity)
            self.assertEqual(service.public_state(names=False, presenter=True)['quiz']['correct'], ['B'])

    def test_participant_http_receives_only_active_question_and_own_receipt(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        participant = fixture.login(fixture.issue('participant', pair=1, member=1))
        operator = {'role': 'organizer', 'credential_id': 'operator'}
        service = workspace.S
        draft = catalog.execute(service, 'save', {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Now?', 'options': ['A', 'B'], 'correct': [1]},
            {'type': 'single', 'prompt': 'Secret future?', 'options': ['C', 'D'], 'correct': [0]},
        ]}, expected_revision=0, request_id='1' * 32, identity=operator)
        catalog.execute(service, 'publish', {'activity_id': draft['id']}, expected_revision=1,
                        request_id='2' * 32, identity=operator)
        run = catalog.execute(service, 'open_run', {'activity_id': draft['id'], 'version': 1}, expected_revision=2,
                              request_id='3' * 32, identity=operator)
        run = catalog.execute(service, 'open_question', {'run_id': run['id'],
                              'question_id': run['snapshot']['questions'][0]['id']}, expected_revision=3,
                              request_id='4' * 32, identity=operator)
        path = '/api/participant/engagement'
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2': '1'}):
            view, _ = fixture.get(path, cookie=participant)
            self.assertEqual(view['question']['questions'][0]['prompt'], 'Now?')
            self.assertNotIn('Secret future?', str(view))
            self.assertNotIn('correct', str(view))
            fixture.get(path, cookie=fixture.admin, status=403)
            receipt, _ = fixture.post(path, {'run_id': run['id'], 'delivery_id': run['delivery']['id'], 'choice': 1}, cookie=participant)
            self.assertTrue(receipt['receipt']['id'])
            view, _ = fixture.get(path, cookie=participant)
            self.assertTrue(view['answered'])
            self.assertEqual(view['selection'], 1)

    def test_answer_command_requires_a_participant_and_commits_a_receipt(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        operator = {'role': 'organizer', 'credential_id': 'operator'}
        participant = {'role': 'participant', 'credential_id': 'participant-1'}
        draft = catalog.execute(service, 'save', {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B'], 'correct': [1]},
        ]}, expected_revision=0, request_id='1' * 32, identity=operator)
        catalog.execute(service, 'publish', {'activity_id': draft['id']}, expected_revision=1,
                        request_id='2' * 32, identity=operator)
        run = catalog.execute(service, 'open_run', {'activity_id': draft['id'], 'version': 1}, expected_revision=2,
                              request_id='3' * 32, identity=operator)
        run = catalog.execute(service, 'open_question', {'run_id': run['id'],
                              'question_id': run['snapshot']['questions'][0]['id']}, expected_revision=3,
                              request_id='4' * 32, identity=operator)
        with self.assertRaises(PermissionError):
            catalog.answer(service, run['id'], run['delivery']['id'], 1, identity=operator)
        receipt = catalog.answer(service, run['id'], run['delivery']['id'], 1, identity=participant)
        self.assertEqual(receipt, catalog.answer(service, run['id'], run['delivery']['id'], 1, identity=participant))
        catalog.execute(service, 'close_question', {'run_id': run['id']}, expected_revision=4,
                        request_id='5' * 32, identity=operator)
        with self.assertRaises(PermissionError):
            catalog.answer(service, run['id'], run['delivery']['id'], 1,
                           identity={**participant, 'impersonation': {'mode': 'preview'}})

    def test_catalog_http_is_feature_gated_and_admin_scoped(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path = '/api/desk/engagement'
        body = {'action': 'save', 'revision': 0, 'request_id': 'd' * 32,
                'payload': {'title': 'Test', 'kind': 'poll', 'questions': [
                    {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}]}}
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2': '0'}):
            fixture.get(path, cookie=fixture.admin, status=404)
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2': '1'}):
            self.assertEqual(fixture.get(path, cookie=fixture.admin)[0]['revision'], 0)
            fixture.post(path, body, cookie=fixture.admin)
            self.assertEqual(fixture.get(path, cookie=fixture.admin)[0]['revision'], 1)
            fixture.post(path, {**body, 'action': [], 'request_id': 'f' * 32}, cookie=fixture.admin, status=400)
            fixture.post(path, {**body, 'revision': 1, 'request_id': 'e' * 32,
                                'payload': {**body['payload'], 'title': '\ud800'}}, cookie=fixture.admin, status=400)
            participant = fixture.login(fixture.issue('participant', pair=1, member=1))
            fixture.get(path, cookie=participant, status=403)
            fixture.post(path, {**body, 'request_id': 'e' * 32}, cookie=participant, status=403)
            guest = fixture.login(workspace.S.CFG['guest_password'])
            fixture.get(path, cookie=guest, status=403)
            fixture.get(path, status=401)
            staff = fixture.login(fixture.issue('desk'))
            fixture.get(path, cookie=staff, headers={'X-Dashboard': 'organizer'})
            fixture.get(path, cookie=staff, headers={'X-Dashboard': 'scoring'}, status=403)
            fixture.post(path, {**body, 'revision': 1, 'request_id': 'a' * 32}, cookie=staff,
                         headers={'X-Dashboard': 'scoring'}, status=403)

    def test_authorized_command_is_audited_and_replay_safe(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        identity = {'role': 'organizer', 'credential_id': 'synthetic-operator', 'label': 'Test'}
        payload = {'title': 'Test', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}
        ]}
        request_id = 'a' * 32
        first = catalog.execute(service, 'save', payload, expected_revision=0,
                                request_id=request_id, identity=identity)
        repeat = catalog.execute(service, 'save', payload, expected_revision=0,
                                 request_id=request_id, identity=identity)
        self.assertEqual(first, repeat)
        self.assertEqual(service.kv_get(catalog.KEY)['revision'], 1)
        self.assertEqual(service.DB.execute("SELECT count(*) FROM audit WHERE action='engagement-save'").fetchone()[0], 1)
        with self.assertRaises(catalog.Conflict):
            catalog.execute(service, 'save', {**payload, 'title': 'Different'}, expected_revision=0,
                            request_id=request_id, identity=identity)
        with self.assertRaises(PermissionError):
            catalog.execute(service, 'save', payload, expected_revision=1, request_id='b' * 32,
                            identity={'role':'participant', 'credential_id':'participant'})
        self.assertEqual(service.kv_get(catalog.KEY)['revision'], 1)

    def test_audit_failure_rolls_back_catalog_and_receipt(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        service.DB.execute("CREATE TRIGGER deny_engagement_audit BEFORE INSERT ON audit WHEN NEW.action='engagement-save' BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        payload = {'title': 'Test', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}
        ]}
        with self.assertRaises(sqlite3.IntegrityError):
            catalog.execute(service, 'save', payload, expected_revision=0, request_id='c' * 32,
                            identity={'role':'organizer', 'credential_id':'synthetic-operator'})
        self.assertIsNone(service.kv_get(catalog.KEY))
        self.assertIsNone(service.kv_get(catalog.RECEIPTS_KEY))

    def test_catalog_writes_follow_live_and_rehearsal_workspaces(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        payload = {'title': 'Live activity', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}
        ]}
        catalog.persist(service, 'save', payload, expected_revision=0)
        fixture.switch('rehearsal')
        self.assertIsNone(service.kv_get(catalog.KEY))
        catalog.persist(service, 'save', {**payload, 'title': 'Practice activity'}, expected_revision=0)
        fixture.switch('live')
        self.assertEqual(service.kv_get(catalog.KEY)['activities'][0]['draft']['title'], 'Live activity')
        fixture.switch('rehearsal')
        self.assertEqual(service.kv_get(catalog.KEY)['activities'][0]['draft']['title'], 'Practice activity')


if __name__ == '__main__':
    unittest.main()
