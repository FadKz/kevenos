"""Bounded agenda timing changes preserve event operations and use exact previews."""
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
import server as S
import event_commands as E
import test_event_commands as W
import test_agent_api as API


class ScheduleRetime(unittest.TestCase):
    setUp = W.EventCommands.setUp
    read = W.EventCommands.read
    call = W.EventCommands.call

    def items(self):
        return self.read('schedule.items')['items']

    def request(self, index=0, shift_following=True, **change):
        return {'change': {'id': self.items()[index]['id'], **change},
                'shift_following': shift_following}

    def preview(self, args):
        return self.read('schedule.retime_preview', **args)

    def apply(self, args):
        plan = self.preview(args)
        self.assertTrue(plan['can_apply'], plan['blockers'])
        return self.call('schedule.retime', **args, plan_digest=plan['plan_digest'])

    def state(self):
        return {table: [list(row) for row in S.DB.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('kv', 'roster', 'roster_members', 'team_assignments', 'sheets', 'judge_invites')}

    def test_duration_extension_and_reduction_preserve_gaps_and_content(self):
        original = deepcopy(S.CONTENT)
        original['agenda'][1]['time'] = '10:05'
        original['agenda'][1]['min'] = 5
        original['agenda'][0]['custom_metadata'] = {'keep': True}
        S.CONTENT = original
        items = self.items()
        args = self.request(duration_minutes=40)
        plan = self.preview(args)
        self.assertTrue(plan['can_apply'])
        self.assertEqual((plan['old_event_end'], plan['new_event_end']), ('14:00', '14:10'))
        self.assertEqual(plan['changes'][1]['after']['start'], '10:15')
        self.assertEqual(plan['changes'][0]['before']['duration_minutes'], 30)
        result = self.apply(args)
        self.assertTrue(result['applied'])
        self.assertEqual(S.CONTENT['agenda'][0]['custom_metadata'], {'keep': True})
        self.assertEqual([x['id'] for x in self.items()], [x['id'] for x in items])
        for before, after in zip(original['agenda'], S.CONTENT['agenda']):
            for key, value in before.items():
                if key not in ('time', 'min'):
                    self.assertEqual(after[key], value)
        for key in ('rubric', 'steps', 'event', 'scenarios', 'judge_rules'):
            self.assertEqual(S.CONTENT[key], original[key])
        self.apply(self.request(duration_minutes=20))
        self.assertEqual(S.CONTENT['agenda'][1]['time'], '09:55')
        self.assertEqual(S.CONTENT['agenda'][-1]['time'], '13:50')

    def test_start_shift_combined_change_and_last_section(self):
        args = self.request(1, start_time='10:05', duration_minutes=15)
        plan = self.preview(args)
        self.assertEqual(plan['changes'][0]['after'], {'start': '10:05', 'end': '10:20', 'duration_minutes': 15})
        self.apply(args)
        self.assertEqual(S.CONTENT['agenda'][2]['time'], '10:20')
        self.apply(self.request(1, shift_minutes=-5))
        self.assertEqual(S.CONTENT['agenda'][1]['time'], '10:00')
        self.assertEqual(S.CONTENT['agenda'][2]['time'], '10:15')
        self.apply(self.request(-1, duration_minutes=30))
        self.assertEqual(S.CONTENT['agenda'][-1]['min'], 30)

    def test_fixed_following_sections_keep_their_times_and_reject_overlap(self):
        later = deepcopy(S.CONTENT['agenda'][1:])
        self.apply(self.request(0, shift_following=False, duration_minutes=20))
        self.assertEqual([{k:v for k,v in item.items() if k!='id'} for item in S.CONTENT['agenda'][1:]], later)
        plan = self.preview(self.request(0, shift_following=False, duration_minutes=35))
        self.assertFalse(plan['can_apply'])
        self.assertIn('overlap', ' '.join(x['message'] for x in plan['blockers']).lower())
        with self.assertRaises(E.CommandError):
            self.call('schedule.retime', **self.request(0, shift_following=False, duration_minutes=35), plan_digest=plan['plan_digest'])

    def test_time_bounds_overlap_and_pitch_capacity_are_blocked(self):
        tests = [self.request(0, shift_minutes=-600),
                 self.request(0, shift_minutes=-580),
                 self.request(0, shift_minutes=900),
                 self.request(1, shift_minutes=-1),
                 self.request(next(i for i,item in enumerate(self.items()) if item['scene']=='pitch'), duration_minutes=59)]
        for args in tests:
            with self.subTest(args=args):
                plan = self.preview(args)
                self.assertFalse(plan['can_apply'])
                self.assertTrue(plan['blockers'])
        last = self.request(-1, start_time='23:30', duration_minutes=30)
        self.assertTrue(self.preview(last)['can_apply'])
        self.assertEqual(self.preview(last)['new_event_end'], '24:00')
        self.assertFalse(self.preview(self.request(-1, start_time='23:30', duration_minutes=31))['can_apply'])

    def test_manual_active_identity_and_all_operational_state_are_preserved(self):
        items = self.items()
        self.call('program.select', action='set', id=items[4]['id'])
        self.call('event.clock', action='start')
        S.kv_set('final', {'status':'prepared','pairs':[1,2],'clock':{'running':True,'elapsed':12}})
        S.kv_set('calib', {'active': True})
        state = self.state()
        raw_program = deepcopy(S.kv_get('program'))
        args = self.request(4, duration_minutes=20)
        plan = self.preview(args)
        self.assertFalse(plan['program']['selection_changes'])
        self.assertEqual(plan['program']['active_item_before']['id'], items[4]['id'])
        self.assertEqual(self.state(), state)
        self.apply(args)
        self.assertEqual(self.state(), state)
        self.assertEqual(S.kv_get('program'), raw_program)

    def test_plan_digest_stable_but_rejects_program_content_config_and_dataset_drift(self):
        args = self.request(duration_minutes=35)
        plan = self.preview(args)
        with patch.object(time, 'time', return_value=time.time()+90):
            self.assertEqual(self.preview(args)['plan_digest'], plan['plan_digest'])
        self.call('program.select', action='set', id=self.items()[1]['id'])
        with self.assertRaisesRegex(E.CommandError, 'Preview'):
            self.call('schedule.retime', **args, plan_digest=plan['plan_digest'])
        plan = self.preview(args)
        S.CONTENT['event']['venue'] = 'Changed fixture venue'
        with self.assertRaisesRegex(E.CommandError, 'Preview'):
            self.call('schedule.retime', **args, plan_digest=plan['plan_digest'])
        plan = self.preview(args)
        self.call('workspace.switch', mode='rehearsal')
        with self.assertRaises(E.CommandError):
            self.call('schedule.retime', **args, plan_digest=plan['plan_digest'])

    def test_preview_does_not_initialize_missing_legacy_configuration(self):
        with S.DB:
            S.DB.execute("DELETE FROM kv WHERE k='event_configuration'")
        before = self.state()
        self.assertTrue(self.preview(self.request(duration_minutes=35))['can_apply'])
        self.assertEqual(self.state(), before)

    def test_schema_requires_intent_and_only_allows_timing_fields(self):
        schema = E.RESOURCES['schedule.retime_preview'][0]['input_schema']
        good = self.request(duration_minutes=35)
        invalid = [dict(good, change={'id':good['change']['id']}),
                   dict(good, change={**good['change'], 'start_time':'10:00','shift_minutes':2}),
                   dict(good, change={**good['change'], 'title':'Do not change this'}),
                   dict(good, change={**good['change'], 'duration_minutes':True}),
                   {'change':good['change']}]
        for item in invalid:
            self.assertFalse(Draft202012Validator(schema).is_valid(item), item)
        command = E.COMMANDS['schedule.retime'][0]
        self.assertEqual(set(command['scope']), {'event:write', 'event:read', 'content:read'})
        self.assertFalse(Draft202012Validator(command['input_schema']).is_valid(good))
        self.assertTrue(Draft202012Validator(command['input_schema']).is_valid(dict(good,plan_digest='a'*64)))

    def test_clock_mode_preview_reports_changed_active_item_without_writing_program(self):
        S.kv_set('program', {'i':0,'mode':'clock','since':123,'announce':'Keep'})
        # 10:07 in the local event clock; adding ten minutes to registration
        # moves the currently scheduled welcome back to registration.
        fixed = time.struct_time((2026,11,13,10,7,0,4,317,-1))
        state = self.state()
        with patch.object(time, 'localtime', return_value=fixed):
            plan = self.preview(self.request(duration_minutes=40))
        self.assertTrue(plan['program']['selection_changes'])
        self.assertTrue(plan['warnings'])
        self.assertEqual(self.state(),state)

    def test_live_and_rehearsal_changes_are_isolated_and_noop_does_not_save(self):
        live = deepcopy(S.CONTENT)
        self.call('workspace.switch', mode='rehearsal')
        self.apply(self.request(duration_minutes=35))
        self.assertEqual(S.CONTENT['agenda'][0]['min'],35)
        self.call('workspace.switch', mode='live')
        self.assertEqual(S.CONTENT,live)
        version = S.CONTENT['content_version']
        result = self.apply(self.request(duration_minutes=30))
        self.assertFalse(result['changed'])
        self.assertFalse(result['applied'])
        self.assertEqual(S.CONTENT['content_version'],version)


class ScheduleRetimeAPI(unittest.TestCase):
    restore = API.AgentAPI.restore
    save_grant = API.AgentAPI.save_grant
    req = API.AgentAPI.req
    status = API.AgentAPI.status
    prepared = API.AgentAPI.prepared

    def setUp(self):
        API.AgentAPI.setUp(self)
        self.principal['scopes'].append('content:read')
        self.save_grant()

    def prepared_timing(self):
        code, value = self.req('read', {'resource':'schedule.items', 'arguments':{}})
        self.assertEqual(code,200,value)
        args = {'change':{'id':value['data']['items'][0]['id'],'duration_minutes':35}, 'shift_following':True}
        code, plan = self.req('read', {'resource':'schedule.retime_preview','arguments':args})
        self.assertEqual(code,200,plan)
        self.assertTrue(plan['data']['can_apply'])
        return self.prepared('schedule.retime', dict(args,plan_digest=plan['data']['plan_digest']))

    def test_timing_only_authority_confirmation_and_durable_replay(self):
        body = self.prepared_timing()
        version = S.CONTENT['content_version']
        code, value = self.req('execute', {k:v for k,v in body.items() if k not in ('confirm','confirmation_id')})
        self.assertEqual((code,value['error']['code']),(409,'confirmation_required'))
        code, value = self.req('execute',body)
        self.assertEqual(code,200,value)
        self.assertEqual(S.CONTENT['content_version'],version+1)
        self.assertEqual(S.CONTENT['agenda'][1]['time'],'10:05')
        code, replay = self.req('execute',body)
        self.assertEqual(code,200,replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(S.CONTENT['content_version'],version+1)
        status = self.status()
        code, denied = self.req('prepare', {'command':'schedule.update_item', 'arguments':{
            'id':body['arguments']['change']['id'],'changes':{'title':'No content write grant'}},
            'context':status['context'],'revision':status['revision']})
        self.assertEqual((code,denied['error']['code']),(403,'scope_forbidden'))

    def test_content_write_followed_by_audit_failure_is_uncertain_and_never_reapplied(self):
        body = self.prepared_timing()
        version = S.CONTENT['content_version']
        with patch.object(S,'audit',side_effect=RuntimeError('Synthetic post-write failure')):
            code, value = self.req('execute',body)
        self.assertEqual((code,value['error']['code']),(500,'outcome_uncertain'))
        self.assertEqual(S.CONTENT['content_version'],version+1)
        code, value = self.req('execute',body)
        self.assertEqual((code,value['error']['code']),(409,'outcome_uncertain'))
        self.assertEqual(S.CONTENT['content_version'],version+1)


if __name__ == '__main__': unittest.main()
