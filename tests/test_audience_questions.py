"""Unscored Audience questions through the real browser and machine routes."""
import unittest
from unittest.mock import patch

import test_agent_api as machine
import test_workspace_routes as workspace

S = workspace.S
DESK = '/api/desk/audience'
PLAYER = '/api/participant/audience'
DRAFT = {'title':'Audience feedback', 'questions':[
    {'type':'rating','prompt':'How useful was the workshop?'},
    {'type':'choice','prompt':'Which example helped?', 'options':[
        {'key':'one','text':'First'}, {'key':'two','text':'Second'}]},
    {'type':'text','prompt':'What should we cover next?'}]}


class AudienceRoutes(unittest.TestCase):
    def setUp(self):
        self.f = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.player = self.f.login(self.f.issue('participant',pair=1,member=1))
        self.staff = self.f.login(self.f.issue('desk'))

    def desk(self):
        return self.f.get(DESK,cookie=self.staff,headers={'X-Dashboard':'organizer'})[0]

    def action(self, action, status=200, **fields):
        headers = {'X-Dashboard':'organizer'}
        current, received = self.f.get(DESK,cookie=self.staff,headers=headers)
        headers['X-Access-Context'] = received['x-access-context'][0]
        payload = {'action':action,'expectedRev':current['rev'],**fields}
        return self.f.post(DESK,payload,cookie=self.staff,headers=headers,status=status)[0]

    def respond(self, pack, question, value, status=200):
        return self.f.post(PLAYER,{'packId':pack,'questionId':question,'value':value},
                           cookie=self.player,status=status)[0]

    def test_pack_workflow_and_score_isolation(self):
        quiz = S.kv_get('event_quiz_v1')
        pack, = self.action('save',pack=DRAFT)['packs']
        pack_id = pack['id']
        rate, choice, free = [q['id'] for q in pack['questions']]
        self.assertEqual(self.f.get(PLAYER,cookie=self.player)[0]['packs'],[])
        self.action('send',packId=pack_id,minutes=0,status=400)
        self.action('send',packId=pack_id)
        from event_push import report
        self.assertEqual([event['kind'] for event in report(S)['events']], ['audience'])
        self.action('save',pack={**DRAFT,'id':pack_id},status=409)
        self.respond(pack_id,rate,True,status=400)
        self.respond(pack_id,rate,5)
        self.respond(pack_id,rate,5)
        self.respond(pack_id,rate,4,status=409)
        self.respond(pack_id,choice,None)
        self.respond(pack_id,free,'Synthetic free text')
        self.assertEqual(self.desk()['packs'][0]['results'][rate]['count'],1)
        current, = self.f.get(PLAYER,cookie=self.player)[0]['packs']
        self.assertEqual(current['answers'],{rate:5, choice:None, free:'Synthetic free text'})
        self.assertNotIn('results',current)
        self.action('release',packId=pack_id,status=409)
        self.action('close',packId=pack_id)
        self.respond(pack_id,rate,5)  # Lost-response retry survives close.
        self.action('release',packId=pack_id)
        self.assertEqual([event['kind'] for event in report(S)['events']], ['results','audience'])
        released, = self.f.get(PLAYER,cookie=self.player)[0]['packs']
        self.assertEqual(released['results'][choice]['count'],0)
        self.assertEqual(released['results'][rate]['counts']['5'],1)
        self.assertEqual(released['results'][free],{'count':1})
        self.assertEqual(S.kv_get('event_quiz_v1'),quiz)

    def test_manual_pack_can_reopen_and_replay_without_overwriting_answers(self):
        pack, = self.action('save',pack=DRAFT)['packs']
        key, question = pack['id'], pack['questions'][0]['id']
        self.assertIsNone(self.action('send',packId=key)['packs'][0]['closesAt'])
        self.respond(key,question,5)
        self.action('close',packId=key)
        reopened, = self.action('resend',packId=key)['packs']
        self.assertIsNone(reopened['closesAt'])
        self.assertEqual(reopened['id'],key)
        self.assertEqual(self.f.get(PLAYER,cookie=self.player)[0]['packs'][0]['answers'],{question:5})
        self.action('close',packId=key)
        self.action('release',packId=key)
        original, replay = self.action('resend',packId=key)['packs']
        self.assertIn('releasedAt',original)
        self.assertNotEqual(replay['id'],key)
        self.assertEqual(replay['results'][question]['count'],0)

    def test_scopes_stale_edits_safety_and_cancel(self):
        self.f.get(DESK,cookie=self.player,status=403)
        self.f.get(DESK,cookie=self.staff,headers={'X-Dashboard':'scoring'},status=403)
        self.f.get(PLAYER,cookie=self.staff,status=403)
        self.action('save',pack={**DRAFT,'questions':[{'type':'rating','prompt':'Bad','points':1}]},status=400)
        stale = self.desk()['rev']
        pack, = self.action('save',pack=DRAFT)['packs']
        self.action('save',expectedRev=stale,pack=DRAFT,status=409)
        key, qid = pack['id'],pack['questions'][0]['id']
        self.action('send',packId=key,minutes=5)
        with patch('event_quiz.quiet',return_value=True):
            self.assertEqual(self.f.get(PLAYER,cookie=self.player)[0]['packs'],[])
            self.respond(key,qid,3,status=409)
        self.respond(key,qid,3)
        cancelled, = self.action('cancel',packId=key)['packs']
        self.assertNotIn('results',cancelled)
        self.action('release',packId=key,status=409)
        self.assertEqual(self.f.get(PLAYER,cookie=self.player)[0]['packs'],[])
        self.f.switch('rehearsal')
        self.assertEqual(self.desk()['packs'],[])
        self.f.switch('live')
        self.assertEqual(self.desk()['packs'][0]['status'],'cancelled')

    def test_draft_edit_id_and_expiry_allow_next_pack(self):
        pack, = self.action('save',pack=DRAFT)['packs']
        qid = pack['questions'][0]['id']
        editing = {'id':pack['id'],'title':'Revised feedback',
                   'questions':[{**pack['questions'][0],'prompt':'How was this session?'},
                                {'type':'rating','prompt':'How was the pace?'}]}
        updated, = self.action('save',pack=editing)['packs']
        self.assertEqual(updated['questions'][0]['id'],qid)
        self.assertNotEqual(updated['questions'][1]['id'],qid)
        other = self.action('save',pack=DRAFT)['packs'][1]
        with patch('audience_questions.now',return_value=60000):
            self.action('send',packId=pack['id'],minutes=1)
            self.action('send',packId=other['id'],status=409)
        with patch('audience_questions.now',return_value=120000):
            self.respond(pack['id'],qid,3,status=409)
            sent = self.action('send',packId=other['id'])['packs']
            self.assertEqual([p['status'] for p in sent],['closed','live'])

    def test_participant_sees_only_own_answers_and_no_quiz_opt_in_needed(self):
        self.f.post('/api/desk/roster',{'rows':[{'pair':1,'partner':'Live Test Company',
                    'p1':'Live Test Person','p2':'Separate Synthetic Person'}]},cookie=self.f.admin)
        other = self.f.login(self.f.issue('participant',pair=1,member=2))
        pack, = self.action('save',pack=DRAFT)['packs']
        key, qid = pack['id'],pack['questions'][2]['id']
        self.action('send',packId=key)
        with patch('event_quiz.prelim_busy',return_value=True):
            self.assertEqual(self.f.get(PLAYER,cookie=self.player)[0]['packs'],[])
            self.respond(key,qid,'Test response',status=409)
        self.respond(key,qid,'Test response')
        original, = self.f.get(PLAYER,cookie=self.player)[0]['packs']
        unrelated, = self.f.get(PLAYER,cookie=other)[0]['packs']
        self.assertEqual(original['answers'],{qid:'Test response'})
        self.assertEqual(unrelated['answers'],{})
        self.action('close',packId=key)
        self.action('release',packId=key)
        released, = self.f.get(PLAYER,cookie=other)[0]['packs']
        self.assertEqual(released['results'][qid],{'count':1})
        self.assertNotIn('Test response',str(released))
        desk, = self.desk()['packs']
        self.assertEqual(desk['results'][qid],{'count':1,'texts':['Test response']})
        self.assertNotIn('Live Test Person',str(desk))

    def test_written_excerpt_budget_does_not_drop_counts_or_attendee_receipts(self):
        self.f.post('/api/desk/roster',{'rows':[{'pair':1,'partner':'Live Test Company',
                    'p1':'Live Test Person','p2':'Separate Synthetic Person'}]},cookie=self.f.admin)
        other = self.f.login(self.f.issue('participant',pair=1,member=2))
        raw = {'title':'Written feedback','questions':[
            {'type':'text','prompt':'What was helpful?'},
            {'type':'text','prompt':'What could change?'}]}
        pack, = self.action('save',pack=raw)['packs']
        key = pack['id']
        first, second = [q['id'] for q in pack['questions']]
        self.action('send',packId=key)
        with patch('audience_questions.QUESTION_EXCERPT_LIMIT',1):
            self.respond(key,first,'First sample')
            self.respond(key,first,'First sample')  # An exact retry cannot double count.
            own, _ = self.f.post(PLAYER,{'packId':key,'questionId':first,'value':'Second sample'},
                                 cookie=other)
        self.assertEqual(own['packs'][0]['answers'],{first:'Second sample'})
        self.respond(key,second,'Another prompt')
        live, = self.desk()['packs']
        self.assertEqual(live['results'][first],{'count':2})
        self.action('close',packId=key)
        with patch('audience_questions.DESK_EXCERPT_LIMIT',1):
            summary, = self.desk()['packs']
        self.assertEqual(summary['results'][first],{'count':2,'texts':['First sample']})
        self.assertEqual(summary['results'][second],{'count':1,'texts':[]})
        url = DESK+f'/responses?packId={key}&questionId={first}&offset=1&limit=1'
        self.f.get(url,cookie=self.player,status=403)
        self.f.get(url,cookie=self.staff,headers={'X-Dashboard':'scoring'},status=403)
        page, _ = self.f.get(url,cookie=self.staff,headers={'X-Dashboard':'organizer'})
        self.assertEqual(page,{'texts':['Second sample'],'total':2,'offset':1,'limit':1})
        self.f.get(url+'&limit=50',cookie=self.staff,headers={'X-Dashboard':'organizer'},status=400)
        self.action('cancel',packId=key)
        self.f.get(url,cookie=self.staff,headers={'X-Dashboard':'organizer'},status=409)


    def test_participant_history_is_bounded_but_desk_keeps_old_packs(self):
        for timestamp in (1000,2000,3000):
            with patch('audience_questions.now',return_value=timestamp):
                pack = self.action('save',pack=DRAFT)['packs'][-1]
                self.action('send',packId=pack['id'])
                self.action('close',packId=pack['id'])
                self.action('release',packId=pack['id'])
        with patch('audience_questions.PARTICIPANT_HISTORY_LIMIT',2):
            recent = self.f.get(PLAYER,cookie=self.player)[0]['packs']
        self.assertEqual([p['closesAt'] for p in recent],[2000,3000])
        self.assertEqual(len(self.desk()['packs']),3)


class AudienceAgent(unittest.TestCase):
    def setUp(self):
        self.f = machine.AgentAPI('test_status_and_capabilities_are_scoped')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def test_confirmation_replay_and_live_revision(self):
        f = self.f
        initial = f.status()['revision']
        body = f.prepared('audience.control',{'action':'save','pack':DRAFT})
        self.assertEqual(f.req('execute',{k:v for k,v in body.items() if k != 'confirm'})[0],409)
        code, result = f.req('execute',body)
        self.assertEqual(code,200,result)
        self.assertNotEqual(result['revision'],initial)
        key = result['data']['pack']['id']
        self.assertTrue(f.req('execute',body)[1]['replayed'])
        code, result = f.req('read',{'resource':'audience.state','arguments':{}})
        self.assertEqual(code,200,result)
        self.assertEqual([p['id'] for p in result['data']['packs']],[key])
        send = f.prepared('audience.control',{'action':'send','packId':key})
        f.principal['scopes'] = ['event:read']
        f.save_grant()
        self.assertEqual(f.req('execute',send)[0],403)
        self.assertEqual(f.req('read',{'resource':'audience.state','arguments':{}})[0],200)
