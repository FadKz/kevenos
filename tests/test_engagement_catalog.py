"""Versioned event-owned engagement drafts and immutable run snapshots."""
import unittest
import json
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


class WorkspaceIsolationTests(unittest.TestCase):
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
