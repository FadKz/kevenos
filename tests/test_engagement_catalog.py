"""Versioned event-owned engagement drafts and immutable run snapshots."""
import unittest
import engagement_catalog as catalog


class EngagementCatalogTests(unittest.TestCase):
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
            {'prompt': 'What is two plus two?', 'type': 'single', 'options': ['3', '4'], 'correct': [1]}
        ]}, expected_revision=0)
        version = catalog.publish(state, draft['id'], expected_revision=1)
        run = catalog.open_run(state, draft['id'], version['version'], expected_revision=2)
        changed = catalog.save_draft(state, {'id': draft['id'], 'title': 'Revised', 'kind': 'quiz', 'questions': [
            {'prompt': 'What is three plus three?', 'type': 'single', 'options': ['5', '6'], 'correct': [1]}
        ]}, expected_revision=3)
        self.assertEqual(changed['title'], 'Revised')
        self.assertEqual(run['snapshot']['title'], 'Session quiz')
        self.assertEqual(run['snapshot']['questions'][0]['prompt'], 'What is two plus two?')
        player = catalog.player_view(run)
        self.assertEqual(player['id'], run['id'])
        self.assertEqual(player['activity_id'], draft['id'])
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


if __name__ == '__main__':
    unittest.main()
