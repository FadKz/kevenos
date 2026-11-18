"""Qualification for rehearsal reconciliation with temporary event databases."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import event_commands
import event_config
import event_profiles
import event_workspace
import rehearsal_reconcile as resize
import server as S
from test_event_access import request


class RehearsalReconcile(unittest.TestCase):
    def setUp(self):
        previous = {key: getattr(S, key) for key in ('CFG', 'DB', 'DATA', 'CONTENT', 'WORKSPACE', 'AUTH_DB', 'LIVE')}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.live_path = self.root / 'judging.db'
        self.addCleanup(lambda: [setattr(S, key, value) for key, value in previous.items()])
        self.addCleanup(lambda: S.WORKSPACE.close())
        self.activate()
        self.assertEqual(event_config.config(S)['participant_count'], 60)
        self.workspace.switch('rehearsal', 'REHEARSAL')

    def activate(self):
        S.DATA = str(self.root)
        S.CFG = S.load_config(str(self.root / 'config.json'))
        S.CONTENT = S.bundled_content()
        S.LIVE = {}
        S.DB = S.open_db(str(self.live_path))
        S.WORKSPACE = event_workspace.initialize(S, self.live_path, self.root)
        self.workspace = S.WORKSPACE

    def target(self, participants=30, size=2, judges=8):
        return {'participant_count': participants, 'team_size': size, 'judge_count': judges}

    def apply(self, target=None):
        target = target or self.target()
        report = resize.preview(S, target)
        self.assertTrue(report['can_apply'], report['blockers'])
        return resize.apply(S, target, report['plan_digest'])

    def rename(self, pair, name='Synthetic real attendee', company='Synthetic company'):
        with S.DB:
            S.DB.execute('UPDATE roster SET p1=?,partner=? WHERE pair=?', (name, company, pair))
            S.DB.execute('UPDATE roster_members SET name=? WHERE pair=? AND member=1', (name, pair))

    def test_seeded_resize_preserves_live_and_epoch_is_durable(self):
        plan = resize.preview(S, self.target())
        self.assertTrue(plan['can_apply'])
        self.assertEqual(plan['summary']['removed_sample_sheets'], 8)
        self.assertEqual(plan['summary']['removed_sample_members'], 30)
        self.assertEqual(plan, resize.preview(S, self.target()))
        live_before = list(self.workspace.live.iterdump())
        old_context = self.workspace.context
        draft_owner = {'station': 1, 'dataset': 'rehearsal', 'credential_id': 'test-judge'}
        draft = event_profiles.draft_key(S, draft_owner)
        result = resize.apply(S, self.target(), plan['plan_digest'])
        self.assertTrue(result['applied'])
        self.assertNotEqual(self.workspace.context, old_context)
        self.assertNotEqual(event_profiles.draft_key(S, draft_owner), draft)
        self.assertEqual(event_config.config(S)['team_count'], 15)
        self.assertEqual(event_config.config(S)['rounds'], 2)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM sheets').fetchone()[0], 0)
        self.assertEqual(S.DB.execute('SELECT max(pair) FROM team_assignments').fetchone()[0], 15)
        self.assertEqual(list(self.workspace.live.iterdump()), live_before)
        self.workspace.close()
        self.activate()
        self.assertEqual(self.workspace.context, result['context'])
        self.assertTrue(resize.apply(S, self.target(), plan['plan_digest'])['replayed'])

    def test_legacy_fixtures_and_growth_do_not_resurrect_old_rows(self):
        with S.DB:
            S.DB.execute("DELETE FROM kv WHERE k='rehearsal_fixtures'")
        self.apply()
        self.apply(self.target(64, 2, 10))
        self.assertEqual(event_config.config(S)['team_count'], 32)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM roster').fetchone()[0], 15)
        self.assertEqual(S.DB.execute('SELECT max(pair) FROM roster_members').fetchone()[0], 15)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM team_assignments').fetchone()[0], 32)
        self.assertEqual(event_config.roster_row(S, 20)['members'][0]['name'], '')

    def test_protected_overflow_team_moves_with_scenario_and_checkin(self):
        self.rename(30)
        S.kv_set('attendees', {
            'directory': {'guest': {'name': 'Example guest'}},
            'participants': {'30:1': {'name': 'Synthetic real attendee', 'checked_in': True, 'email': 'fixture@example.com'}},
        })
        scenario = event_config.assignment_for(S, 30)['scenario']
        content_before = json.dumps(S.CONTENT, sort_keys=True)
        result = self.apply()
        self.assertEqual(result['moves'], [{'from_pair': 30, 'to_pair': 1}])
        row = event_config.roster_row(S, 1)
        self.assertEqual((row['partner'], row['members'][0]['name'], row['scenario']),
                         ('Synthetic company', 'Synthetic real attendee', scenario))
        self.assertIn('1:1', S.kv_get('attendees')['participants'])
        self.assertNotIn('30:1', S.kv_get('attendees')['participants'])
        self.assertEqual(S.kv_get('attendees')['directory']['guest']['name'], 'Example guest')
        self.assertEqual(json.dumps(S.CONTENT, sort_keys=True), content_before)

    def test_active_credentials_anchor_slots_and_stations(self):
        self.rename(1)
        self.rename(30, 'Another attendee')
        db = self.workspace.live
        with db:
            retained = S.event_access.create_credential(S, 'participant', 'Synthetic real attendee', 1, 1)['credential']['id']
            overflow = S.event_access.create_credential(S, 'participant', 'Another attendee', 30, 1)['credential']['id']
            judge = S.event_access.create_credential(S, 'judge', 'Synthetic judge', station=8)['credential']['id']
        target = self.target(judges=7)
        plan = resize.preview(S, target)
        codes = {blocker['code'] for blocker in plan['blockers']}
        self.assertEqual(codes, {'judge_access_outside_capacity', 'participant_access_outside_capacity'})
        with self.assertRaisesRegex(resize.ResizeConflict, 'outside'):
            resize.apply(S, target, plan['plan_digest'])
        with db:
            db.execute('UPDATE access_credentials SET active=0 WHERE id IN (?,?)', (overflow, judge))
        before = list(db.iterdump())
        self.apply(target)
        self.assertEqual(list(db.iterdump()), before)
        self.assertTrue(db.execute('SELECT active FROM access_credentials WHERE id=?', (retained,)).fetchone()[0])
        self.assertEqual(event_config.roster_row(S, 1)['members'][0]['name'], 'Synthetic real attendee')

    def test_edited_sample_score_is_protected_and_keeps_assignment(self):
        with S.DB:
            S.DB.execute("UPDATE sheets SET edited_by='desk',notes='Changed sample result' WHERE id=8")
        bad = resize.preview(S, self.target(judges=7))
        self.assertIn('protected_scores', {item['code'] for item in bad['blockers']})
        good = self.apply()
        self.assertEqual(good['summary']['removed_sample_sheets'], 7)
        self.assertEqual(good['summary']['retained_judging_sheets'], 1)
        self.assertEqual(S.DB.execute('SELECT notes FROM sheets WHERE id=8').fetchone()[0], 'Changed sample result')
        self.assertEqual(event_config.assignment_for(S, 8)['station'], 8)

    def test_stale_plan_and_transaction_rollback(self):
        target = self.target()
        plan = resize.preview(S, target)
        self.rename(30)
        with self.assertRaisesRegex(resize.ResizeConflict, 'Preview the resize again'):
            resize.apply(S, target, plan['plan_digest'])
        plan = resize.preview(S, target)
        before, epoch = list(S.DB.iterdump()), self.workspace.context
        with patch.object(resize.event_config, '_put', side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):
                resize.apply(S, target, plan['plan_digest'])
        self.assertEqual(list(S.DB.iterdump()), before)
        self.assertEqual(self.workspace.context, epoch)
        self.assertTrue(resize.apply(S, target, plan['plan_digest'])['applied'])

    def test_live_and_final_barriers_and_agent_schema(self):
        target = self.target()
        S.kv_set('final', {'status': 'ready'})
        self.assertFalse(resize.preview(S, target)['can_apply'])
        S.kv_set('final', {'status': 'prepared'})
        plan = event_commands.read(S, 'rehearsal.resize_preview', target, {})
        self.assertTrue(plan['can_apply'])
        result = event_commands.execute(S, 'rehearsal.resize_apply', {**target, 'plan_digest': plan['plan_digest']}, {'id': 'synthetic'})
        self.assertTrue(result['applied'])
        self.assertIsNone(S.kv_get('final'))
        self.workspace.switch('live', 'LIVE')
        with self.assertRaisesRegex(resize.ResizeConflict, 'Select the rehearsal'):
            resize.preview(S, target)
        self.assertEqual(event_config.config(S)['participant_count'], 60)

    def test_untouched_migrated_legacy_final_can_be_cleared(self):
        S.kv_set('final', {'active': True, 'pairs': [4, 3, 8], 'current': 0, 'clock': S._fresh_clock()})
        migrated = S.final_event.get(S)
        self.assertTrue(migrated['run_id'].startswith('legacy-'))
        self.assertEqual(migrated['status'], 'ready')
        result = self.apply()
        self.assertTrue(any('migrated Final setup' in warning for warning in result['warnings']))
        self.assertIsNone(S.kv_get('final'))
        self.assertEqual(S.kv_get('reveal'), 'hidden')

    def test_migrated_final_with_judging_evidence_stays_locked(self):
        S.kv_set('final', {'active': True, 'pairs': [4, 3, 8], 'current': 0, 'clock': S._fresh_clock()})
        pristine = S.final_event.get(S)
        for final in (dict(pristine, clock=dict(S._fresh_clock(), elapsed=12)),
                      dict(pristine, clock=dict(S._fresh_clock(), ended=True)),
                      dict(pristine, current=1, visited=[4, 3]),
                      dict(pristine, run_id='1' * 32),
                      dict(pristine, waivers={'4': {'1': {}}})):
            with self.subTest(final=final['run_id'], clock=final['clock']):
                S.kv_set('final', final)
                self.assertFalse(resize.preview(S, self.target())['can_apply'])
        S.kv_set('final', pristine)
        S.audit('desk', 'final-clock', 'start')
        self.assertFalse(resize.preview(S, self.target())['can_apply'])

    def test_browser_route_requires_admin_and_exact_current_plan(self):
        route = '/api/admin/configuration'
        target = self.target()
        with patch.dict('os.environ', {'KEVENOS_HOSTED': '1', 'KEVENOS_GATEWAY_ENABLED': '0'}):
            S.FAILS.clear()
            status, _, _ = request('POST', route + '/preview', target,
                                   headers={'X-Event-Context': self.workspace.context})
            self.assertEqual(status, 401)
            status, _, headers = request('POST', '/api/access/login', {
                'role': 'admin', 'password': S.CFG['password']
            })
            self.assertEqual(status, 200)
            cookie = next(value for value in headers['set-cookie'] if value.startswith('pp_access=')).split(';', 1)[0]
            context = self.workspace.context
            status, plan, _ = request('POST', route + '/preview', target, cookie, {'X-Event-Context': context})
            self.assertEqual(status, 200, plan)
            data = {**target, 'plan_digest': plan['plan_digest']}
            status, result, _ = request('POST', route, data, cookie, {'X-Event-Context': context})
            self.assertEqual(status, 200, result)
            self.assertTrue(result['applied'])
            status, result, _ = request('POST', route, data, cookie,
                                        {'X-Event-Context': context})
            self.assertEqual((status, result['code']), (409, 'workspace_changed'))
            status, _, headers = request('GET', route, cookie=cookie)
            self.assertEqual(status, 200)
            refreshed = headers['x-event-context'][0]
            status, result, _ = request('POST', route, data, cookie, {'X-Event-Context': refreshed})
            self.assertEqual(status, 200, result)
            self.assertTrue(result['replayed'])

    def test_partial_team_reseats_protected_member_and_running_clock_blocks(self):
        self.rename(30)
        target = self.target(3, 2, 2)
        S.kv_set('clock', dict(S._fresh_clock(), running=True))
        plan = resize.preview(S, target)
        self.assertIn('clocks_running', {blocker['code'] for blocker in plan['blockers']})
        S.kv_set('clock', S._fresh_clock())
        self.apply(target)
        self.assertEqual(event_config.config(S)['incomplete_team_size'], 1)
        self.assertEqual(event_config.roster_row(S, 1)['members'][0]['name'], 'Synthetic real attendee')
