"""Participant-only companion projections through the real HTTP boundary."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_workspace_routes as workspace

S = workspace.S


class ParticipantCompanion(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.participant = self.fixture.login(self.fixture.issue('participant', pair=1, member=1))

    def state(self, cookie=None, query=''):
        value, _ = self.fixture.get('/api/participant/state' + query, cookie=cookie or self.participant)
        return value

    def command(self, endpoint, **body):
        value, _ = self.fixture.post('/api/desk/' + endpoint, body, cookie=self.fixture.admin)
        return value

    def sheet(self, pair=1, ticks=6, kind='round', station=1, final_run=''):
        with S.DB:
            S.DB.execute('INSERT INTO sheets(kind,pair,station,round,judge,ticks,ai,spoke,notes,feedback,asked,raised,created,final_run,wrap_rating,final_rating) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (kind, pair, station, 1, 'Private judge identity', json.dumps([1] * ticks + [0] * (10 - ticks)),
                          0, 1, 'PRIVATE NOTES', 'PRIVATE FEEDBACK', '[]', '[]', S.time.time(), final_run, 3 if kind=='round' else 0, 3 if kind=='final' else 0))

    def pitch_program(self):
        self.command('program', action='set', index=next(i for i, row in enumerate(S.agenda()) if row.get('scene') == 'pitch'))

    def test_submitted_team_follows_later_desk_agenda(self):
        self.pitch_program()
        self.sheet()
        S.clock_action(1, 'start', {})
        S.clock_action(1, 'finish', {})
        index = next(i for i, item in enumerate(S.agenda()) if item.get('scene') == 'scores')
        self.command('program', action='set', index=index)
        state = self.state()
        self.assertEqual(state['action']['title'], S.agenda()[index]['title'])
        self.assertEqual(state['action']['code'], 'program')

    def test_session_scopes_assignment_and_poll_has_no_activity_rows(self):
        before = S.DB.execute('SELECT COUNT(*) FROM visitor_activity').fetchone()[0]
        state = self.state(query='?pair=9&member=2')
        self.assertEqual(state['participant']['pair'], 1)
        self.assertEqual(state['participant']['name'], 'Live Test Person')
        self.assertEqual(state['result']['total'], None)
        self.assertEqual(state['program']['title'], 'Registration and coffee')
        self.assertTrue(state['program']['instructions'])
        self.assertIsNone(state['action']['clock'])
        self.assertEqual(before, S.DB.execute('SELECT COUNT(*) FROM visitor_activity').fetchone()[0])

    def test_only_participants_can_read_companion(self):
        self.fixture.get('/api/participant/state', status=401)
        for cookie in (self.fixture.admin, self.fixture.login(self.fixture.issue('desk')),
                       self.fixture.login(S.CFG['guest_password'])):
            self.fixture.get('/api/participant/state', status=403, cookie=cookie)
        self.fixture.get('/api/participant/state', status=403, headers=self.fixture.judge())

    def test_timing_change_exposes_revision_for_current_participant_guide(self):
        before = self.state()
        content = deepcopy(S.CONTENT)
        content['agenda'][-1]['time'] = '14:15'
        version = S.save_content(content, 'Synthetic agenda timing update', 'admin')
        after = self.state()
        event, _ = self.fixture.get('/api/event', cookie=self.participant)
        self.assertEqual(after['participant'], before['participant'])
        self.assertGreater(after['content_version'], before['content_version'])
        self.assertEqual(after['content_version'], version)
        self.assertEqual(event['guide']['content_version'], version)
        self.assertEqual(event['guide']['schedule'][-1]['time'], '14:15')
        self.assertEqual(event['guide']['schedule'][-1]['min'], content['agenda'][-1]['min'])

    def test_room_reveal_does_not_release_private_round_result(self):
        self.fixture.post('/api/desk/roster', {'rows': [{'pair': 1, 'partner': 'Live Test Company', 'p1': 'Live Test Person'},
            {'pair': 2, 'p1': 'PRIVATE OTHER PERSON', 'partner': 'PRIVATE OTHER TEAM'}]}, cookie=self.fixture.admin)
        self.sheet(ticks=6)
        self.sheet(pair=2, ticks=7, station=2)
        self.command('public', mode='off')
        state = self.state()
        self.assertEqual(state['result'], {'kind': 'round', 'status': 'under_review',
                         'label': 'Your round result is under review.', 'total': None, 'rank': None})
        for forbidden in ('PRIVATE', '"judge":', '"notes":', '"feedback":', '"ticks":', '"groups":'):
            self.assertNotIn(forbidden, json.dumps(state))
        self.command('public', mode='live')
        released = self.state()['result']
        self.assertEqual((released['status'], released['total'], released['rank']), ('under_review', None, None))
        self.command('public', mode='off')
        self.assertIsNone(self.state()['result']['total'])
        self.command('reveal', mode='top3')
        self.assertIsNone(self.state()['result']['total'])
        self.sheet(pair=3, ticks=8, station=3)
        self.sheet(pair=4, ticks=9, station=4)
        self.assertIsNone(self.state()['result']['total'])

    def test_station_clock_overrides_stale_program_and_early_receipt(self):
        self.command('mode', mode='self')
        S.clock_action(None, 'goto', {'round': 4})
        S.clock_action(2, 'goto', {'round': 3})
        # Unstarted own station is still registration, regardless of other clocks.
        self.assertEqual(self.state()['action']['code'], 'program')
        self.sheet()
        S.clock_action(1, 'start', {})
        state = self.state()
        self.assertEqual((state['action']['code'], state['action']['clock']['round']), ('pitch', 1))
        S.clock_action(1, 'seek', {'elapsed': 20})
        S.clock_action(1, 'pause', {})
        self.assertEqual(self.state()['action']['code'], 'paused')
        S.clock_action(1, 'start', {})
        S.clock_action(1, 'feedback', {})
        self.assertEqual(self.state()['action']['code'], 'feedback')
        S.clock_action(1, 'finish', {})
        self.assertEqual(self.state()['action']['code'], 'program')

    def test_shared_clock_ready_pause_finished_and_missing_receipt(self):
        self.command('mode', mode='desk')
        self.assertEqual(self.state()['action']['code'], 'program')
        self.pitch_program()
        self.assertEqual(self.state()['action']['code'], 'ready')
        S.clock_action(None, 'start', {})
        self.assertEqual(self.state()['action']['code'], 'pitch')
        S.clock_action(None, 'seek', {'elapsed': 650})
        S.clock_action(None, 'pause', {})
        self.assertEqual(self.state()['action']['code'], 'paused')
        S.clock_action(None, 'finish', {})
        self.assertEqual(self.state()['action']['code'], 'finished')
        self.assertEqual(self.state()['result']['status'], 'awaiting')
        S.clock_action(None, 'next', {})
        state = self.state()
        self.assertEqual(state['action']['code'], 'check_desk')
        self.assertIsNone(state['action']['clock'])

    def test_upcoming_team_stays_at_table_and_calibration_never_claims_live_pitch(self):
        self.fixture.post('/api/desk/roster', {'rows': [{'pair': 1, 'partner': 'Live Test Company', 'p1': 'Live Test Person'},
            {'pair': 9, 'p1': 'Future Participant'}]}, cookie=self.fixture.admin)
        future = self.fixture.login(self.fixture.issue('participant', pair=9, member=1))
        self.pitch_program()
        self.command('mode', mode='self')
        S.clock_action(1, 'start', {})
        state = self.state(future)
        self.assertEqual(state['action']['code'], 'upcoming')
        self.assertEqual((state['action']['station'], state['action']['round']), (1, 2))
        self.assertIsNone(state['action']['clock'])
        S.kv_set('calib', {'active': True})
        self.assertEqual(self.state()['action']['code'], 'program')

    def test_known_legacy_rotation_instruction_is_projected_without_mutating_content(self):
        legacy = 'One bell: stop pitching. Two bells: move to your next station.'
        pitch = next(row for row in S.agenda() if row.get('scene') == 'pitch')
        pitch['do'] = [legacy, 'A custom instruction that stays unchanged.']
        self.pitch_program()
        instructions = self.state()['program']['instructions']
        self.assertNotIn('move to your next station', ' '.join(instructions))
        self.assertEqual(instructions[1], 'A custom instruction that stays unchanged.')
        self.assertEqual(pitch['do'][0], legacy)

    def test_final_actions_hide_selection_then_release_only_frozen_results(self):
        self.sheet()
        self.command('final', action='set', pairs=[1,2,3], panel=[1])
        state = self.state()
        self.assertIsNone(state['final']['qualified'])
        self.assertEqual(state['final']['pairs'], [])
        self.assertEqual(state['result']['kind'], 'round')
        self.command('final', action='start')
        state = self.state()
        self.assertEqual(state['action']['code'], 'final_ready')
        self.assertTrue(state['final']['qualified'])
        self.assertEqual(state['result']['status'], 'awaiting')
        self.command('clock', action='start')
        self.assertEqual(self.state()['action']['code'], 'final_pitch')
        self.command('clock', action='seek', elapsed=30)
        self.command('clock', action='pause')
        self.assertEqual(self.state()['action']['code'], 'final_paused')
        self.command('clock', action='finish')
        self.assertEqual(self.state()['action']['code'], 'final_feedback')
        self.sheet(kind='final', final_run=S.kv_get('final')['run_id'])
        self.assertIsNone(self.state()['result']['total'])
        self.command('final', action='next')
        self.command('clock', action='finish')
        self.sheet(pair=2, kind='final', final_run=S.kv_get('final')['run_id'])
        self.command('final', action='next')
        self.command('clock', action='finish')
        self.sheet(pair=3, kind='final', final_run=S.kv_get('final')['run_id'])
        self.command('final', action='stop')
        self.command('final', action='resolve', order=[1,2,3], reason='Synthetic panel decision for tied star averages.')
        self.assertEqual(self.state()['action']['code'], 'final_review')
        self.command('final', action='publish', confirm='PUBLISH')
        state = self.state()
        awards = next(item for item in S.agenda() if item.get('scene') == 'awards')
        self.assertEqual(state['action']['title'], awards['title'])
        self.assertEqual(state['action']['code'], 'program')
        self.assertEqual((state['result']['status'], state['result']['total'], state['result']['rank']), ('released', 3, 1))
        for forbidden in ('PRIVATE', 'run_id', 'notes', 'receipts', 'missing', 'waivers', 'ticks'):
            self.assertNotIn(forbidden, json.dumps(state))
        with S.DB:
            S.DB.execute("UPDATE sheets SET ticks=? WHERE kind='final'", (json.dumps([1]*10),))
        self.assertEqual(self.state()['result']['total'], 3)
        for scene in ('awards', 'message'):
            index = next(i for i, item in enumerate(S.agenda()) if item.get('scene') == scene)
            self.command('program', action='set', index=index)
            after = self.state()
            self.assertEqual(after['action']['title'], S.agenda()[index]['title'])
            self.assertEqual(after['action']['code'], 'program')
            self.assertEqual(after['result']['total'], 3)
        self.command('reveal', mode='hidden')
        self.assertIsNone(self.state()['result']['total'])
        self.command('final', action='reopen', reason='Correct a mistaken panel score before announcing again.')
        self.assertEqual(self.state()['result']['status'], 'under_review')
        self.assertIsNone(self.state()['result']['total'])

    def test_nonfinalist_keeps_round_result_and_no_stage_clock(self):
        self.sheet()
        self.command('public', mode='live')
        self.command('final', action='set', pairs=[2,3,4], panel=[1])
        self.command('final', action='start')
        self.command('clock', action='start')
        state = self.state()
        self.assertFalse(state['final']['qualified'])
        self.assertEqual(state['action']['code'], 'final_watch')
        self.assertIsNone(state['action']['clock'])
        self.assertEqual((state['result']['kind'], state['result']['total']), ('round', None))

    def test_announcements_have_authored_times_and_clear_cleanly(self):
        initial = S.time.time()
        with patch.object(S.time, 'time', return_value=initial):
            self.command('program', action='announce', text='Welcome teams')
        self.assertEqual(self.state()['announcement'], {'text': 'Welcome teams', 'updated_at': initial})
        with patch.object(S.time, 'time', return_value=initial + 10):
            self.command('program', action='announce', text='Welcome teams')
        self.assertEqual(self.state()['announcement']['updated_at'], initial + 10)
        self.command('program', action='announce', text='')
        self.assertIsNone(self.state()['announcement'])
        self.assertIsNone(S.kv_get('program')['announce_updated_at'])

    def test_workspace_switch_revokes_old_session_and_isolates_announcement_and_scores(self):
        self.sheet(ticks=10)
        self.command('public', mode='live')
        self.command('program', action='announce', text='Live-only announcement')
        live_context = self.fixture.context
        self.fixture.switch('rehearsal')
        self.fixture.get('/api/participant/state', status=401, cookie=self.participant)
        practice = self.fixture.login(self.fixture.issue('participant', pair=1, member=1))
        state, headers = self.fixture.get('/api/participant/state', cookie=practice)
        self.assertTrue(state['rehearsal'])
        self.assertNotEqual(headers['x-event-context'][0], live_context)
        self.assertIsNone(state['announcement'])
        self.assertIsNone(state['result']['total'])
        self.assertNotIn('Live Test Person', json.dumps(state))
        self.command('program', action='announce', text='Practice-only announcement')
        self.fixture.switch('live')
        self.fixture.get('/api/participant/state', status=401, cookie=practice)
        credential = S.AUTH_DB.execute("SELECT id FROM access_credentials WHERE role='participant' AND dataset='live'").fetchone()['id']
        reset, _ = self.fixture.post('/api/admin/credentials', {'action':'reset', 'id':credential}, cookie=self.fixture.admin)
        renewed = self.fixture.login(reset['password'])
        state = self.state(renewed)
        self.assertFalse(state['rehearsal'])
        self.assertEqual(state['announcement']['text'], 'Live-only announcement')
        self.assertIsNone(state['result']['total'])


if __name__ == '__main__':
    unittest.main()
