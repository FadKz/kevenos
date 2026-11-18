"""Cascading structural agenda changes share UI and agent command authority."""
from copy import deepcopy
import json
import unittest
import uuid
from unittest.mock import patch
from jsonschema import Draft202012Validator
import server as S
import event_commands as E
import test_event_commands as W
import test_agent_api as API
import test_event_access as access


class AgendaManagement(unittest.TestCase):
    setUp = W.EventCommands.setUp
    read = W.EventCommands.read
    call = W.EventCommands.call

    def items(self):
        return self.read('schedule.items')['items']

    def preview(self, edit):
        return self.read('schedule.edit_preview', edit=edit)

    def apply(self, edit):
        plan = self.preview(edit)
        self.assertTrue(plan['can_apply'], plan['blockers'])
        return self.call('schedule.edit', edit=edit, plan_digest=plan['plan_digest'])

    def test_insert_break_cascades_preserves_ids_metadata_and_manual_program(self):
        S.CONTENT['agenda'][2]['private_metadata'] = {'keep': True}
        items = self.items()
        self.call('program.select', action='set', id=items[4]['id'])
        self.call('event.clock', action='start')
        program = S.kv_get('program')
        state = S.kv_get('clocks')
        edit = {'action':'insert', 'before_id':items[2]['id'], 'item':{'title':'Coffee break','min':15}}
        before = deepcopy(S.CONTENT)
        plan = self.preview(edit)
        self.assertEqual(S.CONTENT, before)
        self.assertEqual(plan['new_event_end'], '14:15')
        self.assertEqual(plan['changes'][0]['kind'], 'added')
        result = self.apply(edit)
        self.assertEqual(result['plan_digest'], plan['plan_digest'])
        after = self.items()
        self.assertEqual(after[2]['time'], items[2]['time'])
        self.assertEqual(after[3]['time'], '10:25')
        self.assertEqual(S.CONTENT['agenda'][3]['private_metadata'], {'keep':True})
        self.assertEqual([x['id'] for x in after if x['title']!='Coffee break'], [x['id'] for x in items])
        self.assertEqual(S.kv_get('program'), dict(program,i=5,item_id=items[4]["id"]))
        self.assertEqual(S.kv_get('clocks'), state)

    def test_update_and_remove_pull_following_sections_forward(self):
        items = self.items()
        self.apply({'action':'update','id':items[0]['id'],'changes':{'min':20,'title':'Welcome'}})
        self.assertEqual(self.items()[1]['time'],'09:50')
        self.apply({'action':'remove','id':items[1]['id']})
        self.assertEqual(self.items()[1]['time'],'09:50')
        self.assertEqual(self.items()[0]['title'],'Welcome')

    def test_move_preserves_manual_identity_gaps_and_end(self):
        S.CONTENT['agenda'][1].update(time='10:02', min=3)
        items = self.items()
        self.call('program.select', action='set', id=items[1]['id'])
        program = S.kv_get('program')
        self.apply({'action':'move','id':items[1]['id'],'before_id':items[0]['id']})
        after = self.items()
        self.assertEqual(after[0]['id'],items[1]['id'])
        self.assertEqual(after[1]['time'],'09:35')
        self.assertEqual(after[-1]['time'],items[-1]['time'])
        self.assertEqual(S.kv_get('program'),dict(program,i=0,item_id=items[1]["id"]))

    def test_stale_and_unsafe_changes_do_not_write(self):
        items = self.items()
        edits=[{'action':'remove','id':items[0]['id']},
               {'action':'insert','before_id':items[0]['id'],'item':{'title':'Long break','min':1000}},
               {'action':'insert','item':{'title':'Break','min':0}},
               {'action':'update','id':items[1]['id'],'changes':{'time':'09:40'}},
               {'action':'move','id':items[1]['id'],'before_id':'f'*32},
               {'action':'update','id':next(i['id'] for i in items if i['scene']=='pitch'),'changes':{'min':1}}]
        for edit in edits:
            old = deepcopy(S.CONTENT)
            plan = self.preview(edit)
            self.assertFalse(plan['can_apply'], edit)
            with self.assertRaises(E.CommandError):
                self.call('schedule.edit', edit=edit, plan_digest=plan['plan_digest'])
            self.assertEqual(old,S.CONTENT)
        edit={'action':'insert','before_id':items[2]['id'],'item':{'title':'Break','min':10}}
        plan=self.preview(edit)
        self.call('program.select',action='set',id=items[1]['id'])
        with self.assertRaisesRegex(E.CommandError,'Preview again'):
            self.call('schedule.edit',edit=edit,plan_digest=plan['plan_digest'])

    def test_noop_clock_mode_bounds_and_last_section(self):
        items=self.items()
        version=S.CONTENT.get('content_version',1)
        self.assertFalse(self.apply({'action':'move','id':items[0]['id'],'before_id':items[0]['id']})['applied'])
        self.assertEqual(S.CONTENT.get('content_version',1),version)
        self.call('program.mode',mode='clock')
        program=S.kv_get('program')
        result=self.apply({'action':'insert','item':{'title':'Extra discussion','min':10}})
        self.assertEqual(result['new_event_end'],'14:10')
        self.assertEqual(S.kv_get('program'),program)
        self.assertIn('clock',' '.join(result['warnings']).lower())
        self.assertFalse(self.preview({'action':'update','id':items[-1]['id'],'changes':{'time':'23:59','min':2}})['can_apply'])

    def test_rehearsal_change_leaves_live_unchanged(self):
        live=deepcopy(S.CONTENT)
        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        self.apply({'action':'insert','before_id':self.items()[1]['id'],'item':{'title':'Practice break','min':10}})
        S.WORKSPACE.switch('live','LIVE')
        self.assertEqual(S.CONTENT,live)


    def test_interrupted_publish_preserves_manual_selection_and_next_control(self):
        items=self.items()
        self.call('program.select',action='set',id=items[1]['id'])
        since=S.program_now()['since']
        edit={'action':'insert','before_id':items[1]['id'],'item':{'title':'Earlier break','min':10}}
        plan=self.preview(edit)
        with patch.object(S,'audit',side_effect=OSError('synthetic audit failure')):
            with self.assertRaises(OSError):
                self.call('schedule.edit',edit=edit,plan_digest=plan['plan_digest'])
        self.assertEqual(S.program_now()['i'],2)
        self.assertEqual(S.program_now()['since'],since)
        self.assertEqual(self.items()[S.program_now()['i']]['id'],items[1]['id'])
        # After a restart the new file and old numeric index still resolve by ID.
        published=json.loads(__import__('pathlib').Path(S.DATA,'content.json').read_text())
        S.CONTENT=published
        self.assertEqual(self.items()[S.program_now()['i']]['id'],items[1]['id'])
        self.call('program.select',action='next')
        self.assertEqual(self.items()[S.program_now()['i']]['id'],items[2]['id'])

    def test_clock_boundary_invalidates_effect_but_insert_identity_is_stable(self):
        import time
        self.call('program.mode',mode='clock')
        items=self.items()
        edit={'action':'insert','before_id':items[1]['id'],'item':{'title':'Clock break','min':10}}
        with patch('event_schedule.time.localtime',return_value=time.struct_time((2026,11,17,9,59,0,1,321,-1))):
            before=self.preview(edit)
        with patch('event_schedule.time.localtime',return_value=time.struct_time((2026,11,17,10,5,0,1,321,-1))):
            after=self.preview(edit)
            self.assertNotEqual(before['plan_digest'],after['plan_digest'])
            self.assertEqual(before['items'][1]['id'],after['items'][1]['id'])
            with self.assertRaises(E.CommandError):
                self.call('schedule.edit',edit=edit,plan_digest=before['plan_digest'])

    def test_recover_empty_agenda_with_explicit_selection(self):
        selected=self.items()[1]['id']
        S.CONTENT['agenda']=[]
        result=self.call('content.restore',version=1,active_item_id=selected)
        self.assertEqual(result['active_item_id'],selected)

    def test_current_content_changes_are_explicit_and_schema_bounded(self):
        edit={'action':'update','id':self.items()[0]['id'],'changes':{'title':'New welcome','scene':'prep'}}
        plan=self.preview(edit)
        self.assertTrue(any('immediately' in w for w in plan['warnings']))
        self.assertEqual(plan['changes'][0]['details']['scene'],{'before':'info','after':'prep'})
        schema=E.RESOURCES['schedule.edit_preview'][0]['input_schema']
        for value in ({'edit':{'action':'insert','item':{'title':'Break','min':True}}},
                      {'edit':{'action':'update','id':'x','changes':{'min':5}}},
                      {'edit':{'action':'insert','item':{'title':'Break','min':5,'untrusted':'x'}}}):
            self.assertFalse(Draft202012Validator(schema).is_valid(value))
        from agent_mcp import tools_for
        tools=tools_for(E.catalog())
        self.assertIn('schedule_edit_preview',tools)
        self.assertIn('schedule_edit',tools)


class AgendaHTTP(unittest.TestCase):
    setUp=API.AgentAPI.setUp
    restore=API.AgentAPI.restore
    save_grant=API.AgentAPI.save_grant
    req=API.AgentAPI.req
    status=API.AgentAPI.status
    prepared=API.AgentAPI.prepared

    def admin(self):
        code,value,headers=access.request('POST','/api/access/login',{'role':'admin','password':S.CFG['password']},auto_context=False)
        self.assertEqual(code,200,value)
        return next(v for v in headers['set-cookie'] if v.startswith('pp_access=')).split(';')[0]

    def browser(self,body,cookie='',**kw):
        return access.request('POST','/api/admin/agenda',body,cookie=cookie,headers={'X-Event-Context':S.WORKSPACE.context},**kw)[:2]

    def test_admin_exact_preview_apply_and_receipt_replay(self):
        cookie=self.admin()
        code,value=self.browser({'action':'read','resource':'schedule.items','arguments':{}},cookie)
        self.assertEqual(code,200,value)
        edit={'action':'insert','before_id':value['data']['items'][1]['id'],'item':{'title':'Coffee break','min':10}}
        code,preview=self.browser({'action':'read','resource':'schedule.edit_preview','arguments':{'edit':edit}},cookie)
        self.assertEqual(code,200,preview)
        body={'command':'schedule.edit','arguments':{'edit':edit,'plan_digest':preview['data']['plan_digest']},'context':preview['context'],'revision':preview['revision']}
        code,prep=self.browser({'action':'prepare',**body},cookie)
        self.assertEqual(code,200,prep)
        execute={'action':'execute',**body,'confirmation_id':prep['data']['confirmation_id'],'confirm':'EXECUTE','idempotency_key':str(uuid.uuid4())}
        code,result=self.browser(execute,cookie)
        self.assertEqual(code,200,result)
        code,replay=self.browser(execute,cookie)
        self.assertEqual(code,200,replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(sum(x['title']=='Coffee break' for x in S.CONTENT['agenda']),1)
        other=self.admin()
        code,result=self.browser({'action':'receipts','idempotency_key':execute['idempotency_key']},other)
        self.assertEqual(code,404,result)

    def test_browser_route_denies_guest_missing_context_and_unrelated_commands(self):
        read={'action':'read','resource':'schedule.items','arguments':{}}
        self.assertEqual(self.browser(read)[0],401)
        cookie=self.admin()
        self.assertEqual(self.browser({'action':'prepare','command':'program.announce','arguments':{}},cookie)[0],403)
        self.assertEqual(access.request('POST','/api/admin/agenda',read,cookie=cookie,auto_context=False)[0],409)
        with patch.object(S.event_access,'session',return_value={'role':'guest'}):
            from types import SimpleNamespace
            import admin_agenda
            sent=[]
            h=SimpleNamespace(headers={},send=lambda *args:sent.append(args))
            admin_agenda.handle(S,h,'/api/admin/agenda',read)
            self.assertEqual(sent[0][0],403)

    def test_machine_scope_preview_confirmation_and_retry(self):
        self.principal['scopes']=['event:read','content:read']
        self.save_grant()
        _,value=self.req('read',{'resource':'schedule.items','arguments':{}})
        edit={'action':'insert','before_id':value['data']['items'][1]['id'],'item':{'title':'Agent break','min':10}}
        code,preview=self.req('read',{'resource':'schedule.edit_preview','arguments':{'edit':edit}})
        self.assertEqual(code,200,preview)
        args={'edit':edit,'plan_digest':preview['data']['plan_digest']}
        status=self.status()
        code,result=self.req('prepare',{'command':'schedule.edit','arguments':args,'context':status['context'],'revision':status['revision']})
        self.assertEqual(code,403,result)
        self.principal['scopes'].append('content:write');self.save_grant()
        body=self.prepared('schedule.edit',args)
        code,result=self.req('execute',{k:v for k,v in body.items() if k!='confirm'})
        self.assertEqual(code,409,result)
        code,result=self.req('execute',body);self.assertEqual(code,200,result)
        code,replay=self.req('execute',body);self.assertEqual(code,200,replay)
        self.assertTrue(replay['replayed'])


if __name__=='__main__':unittest.main()
