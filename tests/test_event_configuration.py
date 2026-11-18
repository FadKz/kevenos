"""Observable event sizing, member access, and original-assignment guarantees."""
import json
from pathlib import Path
import tempfile
import unittest

import test_event_access as access

S = access.S


class EventConfiguration(unittest.TestCase):
    def setUp(self):
        self.previous = {key: getattr(S, key, None) for key in
                         ('CFG', 'DB', 'DATA', 'CONTENT', 'WORKSPACE', 'AUTH_DB')}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        S.DATA = self.tmp.name
        S.CFG = S.load_config(str(Path(S.DATA) / 'config.json'))
        S.DB = S.open_db(str(Path(S.DATA) / 'judging.db'))
        S.AUTH_DB = None
        S.WORKSPACE = None
        S.CONTENT = S.bundled_content()
        self.addCleanup(self.restore)
        import event_config
        self.config = event_config

    def restore(self):
        S.DB.close()
        for key, value in self.previous.items():
            setattr(S, key, value)

    def test_idle_final_does_not_lock_event_setup(self):
        self.config.activate(S)
        S.kv_set('final', S.final_event.empty())
        self.assertEqual(S.desk_state()['final']['status'], 'idle')
        c = self.config.update(S, {'participant_count': 12, 'team_size': 3, 'judge_count': 4})
        self.assertTrue(c['editable'])
        self.config.save_roster(S, [{'pair': 1, 'members': ['A', 'B', 'C']}])
        self.assertEqual(len(self.config.roster_row(S, 1)['members']), 3)

    def test_setup_changes_clear_prepared_final_snapshots(self):
        self.config.activate(S)
        prepared = {'status': 'prepared', 'pairs': [1, 2], 'panel': list(range(1, 9))}
        S.kv_set('final', prepared)
        result = self.config.update(S, {'participant_count': 12, 'team_size': 3, 'judge_count': 4})
        self.assertIsNone(S.kv_get('final'))
        self.assertTrue(any('finalists' in warning for warning in result['warnings']))
        S.kv_set('final', prepared)
        self.config.save_roster(S, [{'pair': 1, 'members': ['A', 'B', 'C']}])
        self.assertIsNone(S.kv_get('final'))
        self.assertEqual(S.kv_get('reveal'), 'hidden')

    def test_resizing_restarts_paused_pre_scoring_assignments(self):
        self.config.activate(S)
        S.kv_set('mode', 'self')
        S.kv_set('clock', dict(S._fresh_clock(), round=4, elapsed=600, ended=True))
        S.kv_set('sclock:1', dict(S._fresh_clock(), round=4, elapsed=500))
        S.kv_set('sclock:8', dict(S._fresh_clock(), round=3, elapsed=200))
        self.config.update(S, {'participant_count': 12, 'team_size': 3, 'judge_count': 4})
        state = S.judge_state(1)
        self.assertEqual(state['assignment']['pair'], 1)
        self.assertEqual(state['clock']['round'], 1)
        self.assertEqual(state['clock']['elapsed'], 0)
        self.assertFalse(state['clock']['running'])
        self.assertEqual(S.kv_get('clock'), S._fresh_clock())
        self.assertIsNone(S.kv_get('sclock:8'))

    def test_non_default_team_sizes_and_original_scenarios_survive_reschedule(self):
        self.config.activate(S)
        original = self.config.original_scenario(S, 7)['id']
        result = self.config.update(S, {'participant_count': 21, 'team_size': 3, 'judge_count': 3})
        c = result['configuration']
        self.assertEqual((c['team_count'], c['rounds'], c['pitch_block_minutes']), (7, 3, 45))
        self.assertEqual(c['final_panel'], [1, 2, 3])
        self.assertEqual(self.config.assignment_for(S, 7)['station'], 1)
        self.assertEqual(self.config.original_scenario(S, 7)['id'], original)
        self.config.activate(S)
        self.assertEqual(self.config.config(S)['judge_count'], 3)

    def test_extra_member_gets_individual_access_without_changing_existing_credentials(self):
        self.config.update(S, {'participant_count': 6, 'team_size': 3, 'judge_count': 2})
        rows = [{'pair': 1, 'partner': 'Example', 'members': [
            {'id': 1, 'name': 'Person A', 'coach': 1},
            {'id': 2, 'name': 'Person B', 'coach': 1},
            {'id': 3, 'name': 'Person C', 'coach': 1}]}]
        self.config.save_roster(S, rows)
        issued = S.event_access.create_credential(S, 'participant', 'Person C', 1, 3)
        cid = issued['credential']['id']
        before = tuple(S.DB.execute('SELECT lookup,password_hash FROM access_credentials WHERE id=?', (cid,)).fetchone())
        self.config.save_roster(S, rows)
        after = tuple(S.DB.execute('SELECT lookup,password_hash FROM access_credentials WHERE id=? AND active=1', (cid,)).fetchone())
        self.assertEqual(before, after)
        person = S.event_access.participant_assignment(S, {'role': 'participant', 'pair': 1, 'member': 3})
        self.assertEqual(person['name'], 'Person C')
        self.assertEqual(person['teammates'], ['Person A', 'Person B'])
        self.assertEqual(self.config.roster_row(S, 1)['badge'], 4)
        rows[0]['members'][2]['name'] = 'Replacement'
        self.config.save_roster(S, rows)
        self.assertEqual(S.DB.execute('SELECT active FROM access_credentials WHERE id=?', (cid,)).fetchone()[0], 0)

    def test_short_team_is_explicit_and_cannot_exceed_participant_capacity(self):
        c = self.config.update(S, {'participant_count': 11, 'team_size': 3, 'judge_count': 3})['configuration']
        self.assertEqual((c['team_count'], c['incomplete_team_size']), (4, 2))
        self.assertEqual(len(self.config.roster_row(S, 4)['members']), 2)
        with self.assertRaisesRegex(ValueError, 'capacity|members'):
            self.config.save_roster(S, [{'pair': 4, 'members': ['A', 'B', 'C']}])

    def test_larger_panel_and_schedule_warnings_do_not_change_published_times(self):
        before = [item['time'] for item in S.CONTENT['agenda']]
        c = self.config.update(S, {'participant_count': 48, 'team_size': 3, 'judge_count': 12})['configuration']
        self.assertEqual((c['team_count'], c['rounds'], len(c['final_panel'])), (16, 2, 12))
        cases = {self.config.original_scenario(S, pair)['id'] for pair in range(1, 17)}
        self.assertEqual(cases, {'S1', 'S2', 'S3', 'S4'})
        result = self.config.update(S, {'participant_count': 48, 'team_size': 3, 'judge_count': 2})
        self.assertEqual(result['configuration']['pitch_block_minutes'], 120)
        self.assertTrue(any('published block' in message for message in result['warnings']))
        self.assertEqual([item['time'] for item in S.CONTENT['agenda']], before)

    def test_unsafe_resizing_is_rejected_without_touching_scores_or_credentials(self):
        self.config.activate(S)
        self.config.save_roster(S, [{'pair': 1, 'p1': 'Existing A', 'p2': 'Existing B'}])
        with self.assertRaisesRegex(ValueError, 'member|participant'):
            self.config.update(S, {'participant_count': 30, 'team_size': 1, 'judge_count': 8})
        S.DB.execute("INSERT INTO sheets(kind,pair,station,round,ticks) VALUES('round',1,1,1,?)", (json.dumps([1] * 10),))
        S.DB.commit()
        with self.assertRaisesRegex(ValueError, 'scor|sheet'):
            self.config.update(S, {'participant_count': 60, 'team_size': 2, 'judge_count': 4})
        self.assertEqual(S.DB.execute('SELECT count(*) FROM sheets').fetchone()[0], 1)
        self.assertEqual(self.config.config(S)['judge_count'], 8)

    def test_migration_keeps_legacy_people_and_scenario(self):
        S.DB.execute("INSERT INTO roster(pair,partner,p1,p2,coach1,coach2) VALUES(7,'Legacy','First','Second',1,0)")
        S.DB.commit()
        self.config.activate(S)
        row = self.config.roster_row(S, 7)
        self.assertEqual([m['name'] for m in row['members']], ['First', 'Second'])
        self.assertEqual(row['scenario'], 'S4')
        self.assertEqual(row['badge'], 2)


if __name__ == '__main__':
    unittest.main()
