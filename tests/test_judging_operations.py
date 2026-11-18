"""Judging handoffs through the hosted Desk and agent command domain."""
import unittest
from uuid import uuid4

import event_commands
import test_event_access as access

S = access.S


class JudgingOperations(unittest.TestCase):
    def setUp(self):
        fixture = access.EventAccess('test_desk_can_run_live_but_cannot_administer_credentials_or_content')
        self.fixture = fixture
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.desk = fixture.login(fixture.create('desk')['password'])
        S.kv_set('mode', 'desk')

    def post(self, endpoint, status, **body):
        code, result, _ = access.request('POST', '/api/desk/' + endpoint, body, self.desk)
        self.assertEqual(code, status, result)
        return result

    def agent(self, name, **body):
        with S.LOCK:
            return event_commands.execute(S, name, body, {'id': 'rehearsal'})

    def test_desk_and_agent_require_receipts_from_skipped_rounds(self):
        rejected = self.post('clock', 409, action='goto', round=3)
        self.assertEqual({row['round'] for row in rejected['missing']}, {1, 2})
        with self.assertRaisesRegex(event_commands.CommandError, 'sheets'):
            self.agent('event.clock', action='next')
        self.assertEqual(S.clock_public()['round'], 1)

    def test_stale_exception_is_rejected_and_success_is_audited(self):
        first = self.post('clock', 409, action='next')
        self.post('clock', 409, action='goto', round=3,
                  override_reason='Runner verified the missing sheets', review_token=first['review_token'])
        self.post('clock', 200, action='next',
                  override_reason='Runner verified the missing sheets', review_token=first['review_token'])
        self.assertEqual(S.clock_public()['round'], 2)
        self.post('clock', 409, action='next',
                  override_reason='Runner verified the missing sheets', review_token=first['review_token'])
        self.assertTrue(S.DB.execute("SELECT 1 FROM audit WHERE action='preliminary-exception'").fetchone())

    def test_cannot_merge_different_station_rounds_or_move_running_clock(self):
        self.post('clock', 200, action='start')
        self.post('mode', 409, mode='self')
        self.post('clock', 200, action='pause')
        self.post('mode', 200, mode='self')
        S.clock_action(2, 'next', {})
        self.post('mode', 409, mode='desk')
        with self.assertRaisesRegex(event_commands.CommandError, 'same round'):
            self.agent('event.mode', mode='desk')
        self.assertEqual(S.clock_public(2)['round'], 2)


    def test_late_sheet_fills_the_waived_slot_once_without_moving_the_station(self):
        pending = self.post('clock', 409, action='next')
        self.post('clock', 200, action='next', review_token=pending['review_token'],
                  override_reason='Allow the disconnected judge to send their sheet later.')
        judge = self.fixture.login(self.fixture.create('judge', station=1)['password'])
        sheet = {'kind': 'round', 'pair': 1, 'round': 1, 'wrap_rating': 4,
                 'ticks': [True], 'submission_id': str(uuid4())}
        code, first, _ = access.request('POST', '/api/submit', sheet, judge)
        self.assertEqual(code, 200, first)
        code, retry, _ = access.request('POST', '/api/submit', sheet, judge)
        self.assertEqual((code, retry), (200, first))
        code, reject, _ = access.request('POST', '/api/submit', dict(sheet, wrap_rating=5), judge)
        self.assertEqual(code, 409, reject)
        self.assertEqual(S.clock_public(1)['round'], 2)
        self.assertEqual([row['id'] for row in S.active_sheets('round')], [first['id']])

    def test_accepted_round_moves_normally_and_a_stale_click_cannot_move_again(self):
        for station in range(1, S.CFG['stations'] + 1):
            result, code = S.save_sheet(station, {'kind': 'round', 'pair': station,
                                                'wrap_rating': 3}, f'judge{station}')
            self.assertEqual(code, 200, result)
        self.post('clock', 200, action='next', expected_round=1)
        self.post('clock', 409, action='next', expected_round=1)
        self.assertEqual(S.clock_public()['round'], 2)


    def test_only_a_scoring_agent_can_record_an_exception(self):
        with self.assertRaises(event_commands.CommandError) as error:
            self.agent('event.clock', action='next')
        with self.assertRaises(event_commands.CommandError) as rejected:
            self.agent('event.clock', action='next', review_token=error.exception.details['review_token'],
                       override_reason='This provisional request lacks scoring authority.')
        self.assertEqual(rejected.exception.status, 403)
        self.assertEqual(S.clock_public()['round'], 1)

    def test_the_registered_team_must_have_a_receipt_to_start_the_final(self):
        S.DB.execute("INSERT INTO roster(pair, partner, p1) VALUES (1, 'Test team', 'Sample judge fixture')")
        S.DB.commit()
        self.post('final', 200, action='set', pairs=[1, 2, 3], panel=[1])
        missing = self.post('final', 409, action='start')
        self.assertEqual([row['pair'] for row in missing['missing']], [1])
        self.assertEqual(S.save_sheet(1, {'kind': 'round', 'pair': 1, 'wrap_rating': 3}, 'judge1')[1], 200)
        self.post('final', 409, action='start', review_token=missing['review_token'],
                  override_reason='Do not reuse this review once the sheet has arrived.')
        changed = self.post('final', 409, action='start')
        self.assertEqual(changed['code'], 'preliminary_order_changed')
        self.post('final', 200, action='set', pairs=[1, 2, 3], panel=[1])
        ready = self.post('final', 200, action='start')
        self.assertEqual(ready['status'], 'ready')

    def test_hosted_judging_stylesheet_loads_for_both_judging_surfaces(self):
        code, stylesheet, headers = access.request('GET', '/static/judging-operations.css')
        self.assertEqual(code, 200)
        self.assertEqual(headers['content-type'][0].split(';', 1)[0], 'text/css')
        self.assertIn(b'.judging-stations', stylesheet)
