"""App authority is independent of the model, including replay and support mode."""
import json
import time
import unittest
import uuid
from unittest.mock import patch

import event_assistant as A
import test_workspace_routes as W

S=W.S

class Assistant(unittest.TestCase):
    setUp=W.WorkspaceRoutes.setUp
    request=W.WorkspaceRoutes.request
    post=W.WorkspaceRoutes.post
    get=W.WorkspaceRoutes.get
    login=W.WorkspaceRoutes.login
    write_roster=W.WorkspaceRoutes.write_roster
    issue=W.WorkspaceRoutes.issue
    switch=W.WorkspaceRoutes.switch

    def start(self,cookie=None,message='What should I do next?',operation=None):
        with patch.dict('os.environ',{'KEVENOS_ASSISTANT_ENABLED':'1'}),patch.object(A,'spawn'):
            value,_=self.post('/api/assistant/message',{'message':message,'operation_id':operation or str(uuid.uuid4())},cookie=cookie or self.admin)
        return value['thread']['messages'][-1]['job_id']

    def finish(self,jid,proposal=None,answer='Here is the proposed change.'):
        A.finish(S,jid,{'status':'done','terminal':True,'result':json.dumps({'answer':answer,'proposed_action':proposal})})

    def proposal(self,jid,cookie=None):
        value,_=self.get('/api/assistant',cookie=cookie or self.admin)
        return next(m['proposal'] for m in value['thread']['messages'] if m['id']==jid)

    def test_audience_actions_are_offered_only_to_event_operations(self):
        for role in ('admin', 'desk', 'organizer'):
            self.assertIn('audience.control', A.commands(role))
        for role in ('scoring', 'administration'):
            self.assertNotIn('audience.control', A.commands(role))
        context = A.context(S, 'organizer', [], 'Create a three-question audience sentiment pack')
        self.assertIn('audience.control', {entry['name'] for entry in context['commands']})
        self.assertIn('audience_questions', context['event'])
        self.assertIn('no points', context['event']['help'])

    def test_audience_pack_proposal_requires_confirmation_and_replays_once(self):
        import audience_questions as Q
        pack = {'title':'Room pulse','questions':[{'type':'rating','prompt':'Confidence from 1 to 5?'}]}
        jid = self.start(message='Draft an unscored room pulse')
        self.finish(jid, {'command':'audience.control','arguments':{'action':'save','pack':pack}})
        prop = self.proposal(jid)
        self.assertEqual(Q.load(S)['packs'], [])
        self.post('/api/assistant/confirm', {'proposal_id':prop['id']}, cookie=self.admin)
        self.post('/api/assistant/confirm', {'proposal_id':prop['id']}, cookie=self.admin)
        state = Q.load(S)
        self.assertEqual(len(state['packs']),1)
        self.assertEqual(state['packs'][0]['status'],'draft')
        jid = self.start(message='Send the approved room pulse for five minutes')
        self.finish(jid, {'command':'audience.control','arguments':{'action':'send','packId':state['packs'][0]['id'],'minutes':5}})
        prop = self.proposal(jid)
        self.assertEqual(Q.load(S)['packs'][0]['status'],'draft')
        self.post('/api/assistant/confirm', {'proposal_id':prop['id']}, cookie=self.admin)
        self.assertEqual(Q.load(S)['packs'][0]['status'],'live')

    def test_answer_and_context_have_no_secrets_or_personal_roster(self):
        jid=self.start()
        row=A.db(S).execute('SELECT * FROM assistant_jobs WHERE id=?',(jid,)).fetchone()
        context=json.loads(row['request'])['context']
        self.assertLess(len(json.dumps(context).encode()),42000)
        for secret in (S.CFG['password'],S.CFG['secret'],'Live Test Person'):
            self.assertNotIn(secret,row['request'])
        self.finish(jid,answer='The Final uses the original scenario.')
        value,_=self.get('/api/assistant',cookie=self.admin)
        self.assertEqual(value['thread']['messages'][-1]['status'],'completed')
        self.assertIn('original scenario',value['thread']['messages'][-1]['text'])

    def test_guest_and_judge_denied(self):
        guest=self.login(S.CFG['guest_password'])
        for method,path,body in [('GET','/api/assistant',None),('POST','/api/assistant/message',{'message':'Hi','operation_id':str(uuid.uuid4())})]:
            self.assertEqual(self.request(method,path,body,cookie=guest)[0],403)
        self.assertEqual(self.request('GET','/api/assistant')[0],401)

    def test_no_action_without_confirm_and_replay_exact_once(self):
        jid=self.start(message='Announce a rehearsal welcome')
        self.finish(jid,{'command':'program.announce','arguments':{'text':'Rehearsal welcome'}})
        prop=self.proposal(jid)
        self.assertNotEqual(S.kv_get('program',{}).get('announce'),'Rehearsal welcome')
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin)
        self.assertEqual(S.kv_get('program')['announce'],'Rehearsal welcome')
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin)
        self.assertEqual(A.db(S).execute('SELECT count(*) FROM agent_receipts').fetchone()[0],1)

    def test_desk_cannot_propose_admin_actions_and_sessions_are_private(self):
        desk=self.login(self.issue('desk'))
        jid=self.start(cookie=desk)
        self.finish(jid,{'command':'event.configure','arguments':{'participant_count':30,'team_size':2,'judge_count':8}})
        value,_=self.get('/api/assistant',cookie=desk)
        self.assertNotIn('proposal',value['thread']['messages'][-1])
        admin,_=self.get('/api/assistant',cookie=self.admin)
        self.assertEqual(admin['thread']['messages'],[])
        self.post('/api/assistant/confirm',{'proposal_id':'not-owned'},cookie=self.admin,status=404)

    def test_stale_revision_and_changed_payload_refused(self):
        op=str(uuid.uuid4());jid=self.start(operation=op)
        self.assertEqual(self.start(operation=op),jid)
        with patch.dict('os.environ',{'KEVENOS_ASSISTANT_ENABLED':'1'}),patch.object(A,'spawn'):
            self.post('/api/assistant/message',{'message':'Different','operation_id':op},cookie=self.admin,status=409)
        self.finish(jid,{'command':'program.announce','arguments':{'text':'Old'}})
        prop=self.proposal(jid)
        S.kv_set('program',{'announce':'New','i':0,'mode':'manual'})
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin,status=409)
        self.assertEqual(S.kv_get('program')['announce'],'New')

    def test_preview_can_chat_but_never_confirm(self):
        _,profile,_=self.request('POST','/api/access/impersonate',{'role':'desk','mode':'preview'},cookie=self.admin)
        headers={'X-Profile-Token':profile['profile_token']}
        with patch.dict('os.environ',{'KEVENOS_ASSISTANT_ENABLED':'1'}),patch.object(A,'spawn'):
            value,_=self.post('/api/assistant/message',{'message':'Explain Desk','operation_id':str(uuid.uuid4())},cookie=self.admin,headers=headers)
        self.assertTrue(value['preview'])
        jid=value['thread']['messages'][-1]['job_id']
        self.finish(jid,{'command':'program.announce','arguments':{'text':'No'}})
        value,_=self.get('/api/assistant',cookie=self.admin,headers=headers)
        prop=value['thread']['messages'][-1]['proposal']
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin,headers=headers,status=403)
        self.assertNotEqual(S.kv_get('program',{}).get('announce'),'No')

    def test_cancel_reset_and_expiry(self):
        jid=self.start(); self.finish(jid,{'command':'program.announce','arguments':{'text':'Never'}})
        prop=self.proposal(jid)
        self.post('/api/assistant/cancel',{'proposal_id':prop['id']},cookie=self.admin)
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin,status=409)
        value,_=self.post('/api/assistant/reset',{},cookie=self.admin)
        self.assertEqual(value['thread']['messages'],[])
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin,status=409)

    def test_resize_proposal_uses_deterministic_preview(self):
        self.switch('rehearsal')
        jid=self.start(message='Reduce rehearsal to 30 people')
        self.finish(jid,{'command':'event.configure','arguments':{'participant_count':30,'team_size':2,'judge_count':8}})
        prop=self.proposal(jid)
        self.assertEqual(prop['command'],'rehearsal.resize_apply')
        self.assertTrue(prop['arguments']['plan_digest'])
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=self.admin)
        self.assertEqual(S.CFG['participant_count'],30)

    def test_provider_encodes_real_event_punctuation_as_utf8(self):
        from unittest.mock import MagicMock
        transport=MagicMock()
        transport.getresponse.return_value.status=200
        transport.getresponse.return_value.read.return_value=b'{}'
        with patch.object(A.Path,'read_text',return_value='synthetic-caller'),patch.object(A,'SocketConnection',return_value=transport):
            A.provider('POST','/v1/jobs',{'prompt':'Programme — مرحبا'})
        sent=transport.request.call_args.args[2]
        self.assertIsInstance(sent,bytes)
        self.assertEqual(json.loads(sent),{'prompt':'Programme — مرحبا'})

    def test_provider_operations_are_namespaced_by_signin(self):
        op=str(uuid.uuid4()); admin_job=self.start(operation=op)
        desk=self.login(self.issue('desk'))
        desk_job=self.start(operation=op,cookie=desk)
        rows=[json.loads(A.db(S).execute('SELECT request FROM assistant_jobs WHERE id=?',(j,)).fetchone()[0]) for j in (admin_job,desk_job)]
        self.assertNotEqual(rows[0]['operation_id'],rows[1]['operation_id'])
        self.assertNotEqual(rows[0]['operation_id'],op)
        with patch.object(A,'provider',side_effect=A.ProviderError(409)) as transport:
            A.run(S,desk_job)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(A.db(S).execute('SELECT status FROM assistant_jobs WHERE id=?',(desk_job,)).fetchone()[0],'failed')

    def test_response_cannot_smuggle_extra_parameters_or_credentials(self):
        jid=self.start()
        self.finish(jid,{'command':'program.announce','arguments':{'text':'Hello','role':'admin'}})
        value,_=self.get('/api/assistant',cookie=self.admin)
        self.assertNotIn('proposal',value['thread']['messages'][-1])
        with patch.dict('os.environ',{'KEVENOS_ASSISTANT_ENABLED':'1'}):
            self.post('/api/assistant/message',{'message':'My key '+S.CFG['password'],'operation_id':str(uuid.uuid4())},cookie=self.admin,status=400)

    def test_support_action_preserves_human_and_profile_audit_identity(self):
        _, profile, _ = self.request('POST', '/api/access/impersonate',
            {'role': 'desk', 'mode': 'support'}, cookie=self.admin)
        headers = {'X-Profile-Token': profile['profile_token']}
        identity = S.event_access.session(S, {'Cookie': self.admin, **headers})
        with patch.dict('os.environ', {'KEVENOS_ASSISTANT_ENABLED': '1'}), patch.object(A, 'spawn'):
            value, _ = self.post('/api/assistant/message',
                {'message': 'Announce the synthetic audit check', 'operation_id': str(uuid.uuid4())},
                cookie=self.admin, headers=headers)
        jid = value['thread']['messages'][-1]['job_id']
        self.finish(jid, {'command': 'program.announce', 'arguments': {'text': 'Synthetic audit check'}})
        value, _ = self.get('/api/assistant', cookie=self.admin, headers=headers)
        proposal = value['thread']['messages'][-1]['proposal']
        self.post('/api/assistant/confirm', {'proposal_id': proposal['id']},
                  cookie=self.admin, headers=headers)
        for action in ('program-announce', 'assistant-action', 'profile-operation'):
            with self.subTest(action=action):
                row = S.DB.execute('SELECT * FROM audit WHERE action=? ORDER BY id DESC LIMIT 1',
                                   (action,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row['actor_credential'], identity['actor']['credential_id'])
                self.assertEqual(row['effective_credential'], identity['credential_id'])
                self.assertEqual(row['effective_role'], 'organizer' if action=='program-announce' else 'desk')
                self.assertEqual(row['profile_id'], identity['impersonation']['id'])
                self.assertEqual(row['impersonation_mode'], 'support')

    def direct(self, message, cookie=None, operation=None):
        with patch.dict('os.environ', {'KEVENOS_ASSISTANT_ENABLED': '1'}), patch.object(A, 'spawn') as worker, patch.object(A, 'provider') as provider:
            value, _ = self.post('/api/assistant/message', {'message': message, 'operation_id': operation or str(uuid.uuid4())}, cookie=cookie or self.admin)
        worker.assert_not_called()
        provider.assert_not_called()
        reply = value['thread']['messages'][-1]
        self.assertEqual(reply['status'], 'completed')
        self.assertEqual(reply['source'], 'event')
        self.assertGreater(reply['created_at'], 0)
        self.assertNotIn('proposal', reply)
        return reply

    def test_simple_counts_are_immediate_fresh_and_replay_the_same_answer(self):
        operation = str(uuid.uuid4())
        first = self.direct('How many participants do we have now ?', operation=operation)
        self.assertIn('60 participants', first['text'])
        self.assertIn('configured', first['text'])
        self.post('/api/admin/configuration', {'participant_count': 30, 'team_size': 2, 'judge_count': 8}, cookie=self.admin)
        replay = self.direct('How many participants do we have now ?', operation=operation)
        self.assertEqual(first, replay)
        fresh = self.direct('How many participants are there?')
        self.assertIn('30 participants', fresh['text'])
        self.assertEqual(A.db(S).execute('SELECT count(*) FROM agent_receipts').fetchone()[0], 0)

    def test_admin_judge_names_use_current_active_accounts_and_show_unassigned(self):
        self.issue('judge', station=1, label='Synthetic Named Judge')
        with S.DB:
            S.DB.execute('INSERT INTO judge_invites(station,digest,judge,expires,used) VALUES(1,?,?,?,0)', ('synthetic-digest', 'Old Invite Name', time.time()+600))
        reply = self.direct('Who are the Judges ?')
        self.assertIn('Station 1: Synthetic Named Judge', reply['text'])
        self.assertIn('Station 8: Unassigned', reply['text'])
        self.assertNotIn('Old Invite Name', reply['text'])
        self.assertNotIn('synthetic-digest', reply['text'])
        self.switch('rehearsal')
        reply = self.direct('Who are the judges?')
        self.assertNotIn('Synthetic Named Judge', reply['text'])
        self.assertIn('Rehearsal', reply['text'])
        self.assertIn('Station 1: Unassigned', reply['text'])

    def test_organizer_judge_read_does_not_inherit_admin_account_access(self):
        self.issue('judge', station=1, label='Private Assignment Label')
        desk = self.login(self.issue('desk'))
        S.LIVE[1] = {'judge': 'Visible Desk Judge', 't': time.time()}
        self.addCleanup(S.LIVE.clear)
        reply = self.direct('Who are the judges?', cookie=desk)
        self.assertIn('Visible Desk Judge', reply['text'])
        self.assertNotIn('Private Assignment Label', reply['text'])
        self.assertIn('observed', reply['text'])
        self.assertNotIn('Unassigned', reply['text'])
        context = A.context(S, 'desk', [], 'Explain the judging assignments')
        self.assertNotIn('Private Assignment Label', json.dumps(context))
        self.assertIn('Visible Desk Judge', json.dumps(context))

    def test_all_capacity_facts_preserve_domain_state(self):
        before = A.encoded(A.event_commands.snapshot(S))
        for question, expected in (('How many teams do we have?', '30 teams'),
                                   ('How many judges are there?', '8 judges'),
                                   ('How many rounds are there?', '4 preliminary'),
                                   ('What is the team size?', '2 participants per team')):
            with self.subTest(question=question):
                self.assertIn(expected, self.direct(question)['text'])
        self.assertEqual(before, A.encoded(A.event_commands.snapshot(S)))

    def test_factual_answer_is_available_while_model_jobs_are_busy(self):
        self.start(message='Explain the Final workflow')
        self.start(message='Explain the judging rules')
        answer = self.direct('Please tell me who are the judges?')
        self.assertIn('Station 8: Unassigned', answer['text'])
        with patch.dict('os.environ', {'KEVENOS_ASSISTANT_ENABLED': '1'}), patch.object(A, 'spawn'):
            self.post('/api/assistant/message', {'message': 'Explain the participant instructions', 'operation_id': str(uuid.uuid4())}, cookie=self.admin, status=429)

    def test_facts_keep_conversation_rate_limit_and_cannot_execute_compound_request(self):
        for message in ('How many teams?', 'How many participants?', 'Who are our judges?', 'How many judges?'):
            self.direct(message)
        with patch.dict('os.environ', {'KEVENOS_ASSISTANT_ENABLED': '1'}):
            self.post('/api/assistant/message', {'message': 'How many rounds?', 'operation_id': str(uuid.uuid4())}, cookie=self.admin, status=429)
        self.assertIsNone(A.factual_answer(S, 'admin', 'How many participants? Then set it to 30.'))
        self.assertIsNone(A.factual_answer(S, 'admin', 'Who are the judges and send their passwords?'))

    def test_question_context_omits_commands_but_action_requests_keep_them(self):
        self.issue('judge', station=1, label='Synthetic Named Judge')
        question = A.context(S, 'admin', [], 'Explain the Final workflow')
        action = A.context(S, 'admin', [], 'Change the participant count to 30')
        self.assertEqual(question['commands'], [])
        self.assertIn('Synthetic Named Judge', json.dumps(question))
        self.assertTrue(action['commands'])
        self.assertLess(len(A.encoded(question)), len(A.encoded(action)) * .65)
        self.assertTrue(A.context(S, 'admin', [], 'Explain the Final and then start it')['commands'])
        with patch.dict('os.environ', {'KEVENOS_ASSISTANT_ENABLED': '1'}), patch.object(A, 'spawn') as worker:
            self.post('/api/assistant/message', {'message': 'Who are the judges and change them?', 'operation_id': str(uuid.uuid4())}, cookie=self.admin)
        worker.assert_called_once()

    def test_compound_questions_retain_action_catalog(self):
        prompts = (
            'Explain the Final workflow, then display a welcome message on the live screen.',
            'What is the Final workflow? Also correct sheet 1.',
            'Who are the judges? Rename team 1 to Example Team.',
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(A.context(S, 'admin', [], prompt)['commands'])
        self.assertEqual(A.context(S, 'admin', [], 'Explain the Final workflow')['commands'], [])

    def test_organizer_timing_proposal_previews_impact_and_confirms_once(self):
        import event_commands as E
        from copy import deepcopy
        desk=self.login(self.issue('desk'))
        S.kv_set('program', {'i':0,'mode':'manual','since':100.0,'announce':''})
        before=deepcopy(S.CONTENT)
        item=E.read(S,'schedule.items',{}, {})['items'][0]
        jid=self.start(cookie=desk,message='Extend registration by ten minutes and move the remaining agenda')
        row=A.db(S).execute('SELECT request FROM assistant_jobs WHERE id=?',(jid,)).fetchone()
        offered=next(c for c in json.loads(row['request'])['context']['commands'] if c['name']=='schedule.retime')
        self.assertNotIn('plan_digest',offered['input_schema']['properties'])
        args={'change':{'id':item['id'],'duration_minutes':item['min']+10},'shift_following':True}
        self.finish(jid,{'command':'schedule.retime','arguments':args})
        prop=self.proposal(jid,cookie=desk)
        self.assertEqual(S.CONTENT,before)
        self.assertIn('plan_digest',prop['arguments'])
        self.assertTrue(any('09:30' in e and '10:10' in e for e in prop['effects']))
        self.assertTrue(any('Event finish:' in e for e in prop['effects']))
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=desk)
        self.assertEqual(S.CONTENT['agenda'][0]['min'],item['min']+10)
        self.assertEqual(S.CONTENT['agenda'][1]['time'],'10:10')
        self.assertEqual(S.kv_get('program')['since'],100.0)
        version=S.CONTENT['content_version']
        self.post('/api/assistant/confirm',{'proposal_id':prop['id']},cookie=desk)
        self.assertEqual(S.CONTENT['content_version'],version)
        self.assertNotIn('schedule.replace', A.commands('desk'))
        self.assertNotIn('schedule.update_item', A.commands('desk'))

    def test_untrusted_agent_cannot_mutate_by_claiming_success(self):
        jid=self.start(); self.finish(jid,None,'I changed the event.')
        self.assertEqual(A.db(S).execute('SELECT count(*) FROM agent_receipts').fetchone()[0],0)
        self.assertEqual(S.CFG['participant_count'],60)

if __name__=='__main__': unittest.main()
