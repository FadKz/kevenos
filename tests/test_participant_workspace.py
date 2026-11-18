"""Team workspace contracts exercised through the application HTTP handler."""
import unittest

import test_workspace_routes as workspace

S = workspace.S


class ParticipantWorkspace(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        f = self.fixture
        f.post('/api/desk/roster', {'rows': [
            {'pair': 1, 'partner': 'Example Partner', 'p1': 'One', 'p2': 'Two'},
            {'pair': 2, 'partner': 'Other Partner', 'p1': 'Three'}
        ]}, cookie=f.admin)
        self.one = f.login(f.issue('participant', pair=1, member=1))
        self.two = f.login(f.issue('participant', pair=1, member=2))
        self.other = f.login(f.issue('participant', pair=2, member=1))

    def hub(self, cookie):
        return self.fixture.get('/api/participant/workspace', cookie=cookie)[0]

    def test_shared_notes_and_conflicts(self):
        f = self.fixture
        team = self.hub(self.one)['team']
        body = {'action': 'save_step', 'team': team, 'step': 'discover',
                'revision': 0, 'cues': 'Ask who decides', 'speaker': 'One'}
        f.post('/api/participant/workspace', body, cookie=self.one)
        self.assertEqual(self.hub(self.two)['steps']['discover']['cues'], 'Ask who decides')
        f.post('/api/participant/workspace', dict(body, cues='Stale edit'), cookie=self.two, status=409)
        f.post('/api/participant/workspace', dict(body, revision=1), cookie=self.other, status=409)
        self.assertEqual(self.hub(self.other)['steps'], {})
        f.get('/api/participant/workspace', headers=f.judge(), status=403)

    def test_replaced_roster_does_not_inherit_work_or_approval(self):
        f = self.fixture
        team = self.hub(self.one)['team']
        f.post('/api/participant/workspace', {'action': 'save_step', 'team': team, 'step': 'close',
                'revision': 0, 'cues': 'Private team cue', 'speaker': ''}, cookie=self.one)
        f.post('/api/submit', {'kind': 'round', 'pair': 1, 'wrap_rating': 4, 'ticks': [1] * 8,
                'feedback': 'Old team feedback.', 'participant_feedback_approved': True}, headers=f.judge())
        f.post('/api/desk/roster', {'rows': [
            {'pair': 1, 'partner': 'Another Partner', 'p1': 'Replacement'}
        ]}, cookie=f.admin)
        f.get('/api/participant/workspace', cookie=self.one, status=401)
        replacement = self.hub(f.login(f.issue('participant', pair=1, member=1)))
        self.assertNotEqual(replacement['team'], team)
        self.assertEqual(replacement['steps'], {})
        entries = f.get('/api/desk/participant-workspace', cookie=f.admin)[0]['review']['entries']
        self.assertEqual(entries[0]['status'], 'needs_approval')

    def test_nominations_and_participant_feed(self):
        f = self.fixture
        body = {'action': 'submit_nominations', 'team': self.hub(self.one)['team'],
                'revision': 0, 'companies': [{'name': name, 'why': 'Laptop refresh this year.'}
                                            for name in ('Example A', 'Example B', 'Example C')]}
        f.post('/api/participant/workspace', body, cookie=self.one, status=409)
        f.post('/api/desk/participant-workspace', {'action': 'nominations', 'open': True}, cookie=f.admin)
        f.post('/api/participant/workspace', dict(body, companies=body['companies'][:2]), cookie=self.one, status=400)
        f.post('/api/participant/workspace', dict(body, companies=[body['companies'][0]] * 3), cookie=self.one, status=400)
        f.post('/api/participant/workspace', body, cookie=self.one)
        self.assertEqual(self.hub(self.two)['nomination']['companies'], body['companies'])
        self.assertIsNone(self.hub(self.other)['nomination']['submitted_at'])
        f.post('/api/desk/participant-workspace', {'action': 'post_news', 'text': 'Go when called.', 'pinned': True}, cookie=f.admin)
        self.assertEqual(self.hub(self.other)['news'][0]['text'], 'Go when called.')
        f.get('/api/desk/participant-workspace', cookie=self.one, status=403)
        f.switch('rehearsal')
        f.get('/api/participant/workspace', cookie=self.one, status=401)
        practice = f.login(f.issue('participant', pair=1, member=1))
        self.assertIsNone(self.hub(practice)['nomination']['submitted_at'])
        self.assertEqual(self.hub(practice)['news'], [])

    def test_result_requires_judge_approval_and_desk_release(self):
        f = self.fixture
        judge = f.judge()
        sheet = {'kind': 'round', 'pair': 1, 'wrap_rating': 4, 'ticks': [1] * 8,
                 'notes': 'Private note', 'feedback': 'Ask for a date.'}
        f.post('/api/submit', sheet, headers=judge)
        path = '/api/desk/participant-workspace'
        pitch_end = max(i for i, item in enumerate(S.agenda()) if item.get('scene') == 'pitch')
        f.post('/api/desk/program', {'action': 'set', 'index': pitch_end + 1}, cookie=f.admin)
        review = f.get(path, cookie=f.admin)[0]['review']
        f.post(path, {'action': 'release', 'digest': review['digest']}, status=409, cookie=f.admin)
        f.post('/api/submit', dict(sheet, participant_feedback_approved=True), headers=judge)
        self.assertIsNone(self.hub(self.one)['preliminary'])
        review = f.get(path, cookie=f.admin)[0]['review']
        f.post(path, {'action': 'release', 'digest': review['digest']}, cookie=f.admin)
        released = self.hub(self.two)['preliminary']
        self.assertEqual((released['total'], released['feedback']), (80, 'Ask for a date.'))
        self.assertNotIn('Private note', str(released))
        self.assertIsNone(self.hub(self.other)['preliminary'])
        f.post('/api/submit', dict(sheet, feedback='Updated.', participant_feedback_approved=True), headers=judge)
        self.assertEqual(self.hub(self.one)['preliminary'], {'status': 'corrected'})
