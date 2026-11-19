"""Machine credentials and durable command envelopes, through the real HTTP handler."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import uuid
from unittest.mock import patch
import test_event_access as access
import event_workspace
S = access.S

class AgentAPI(unittest.TestCase):
    def setUp(self):
        self.before={k:getattr(S,k) for k in ('CFG','DB','DATA','CONTENT','AUTH_DB','WORKSPACE')}
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        S.DATA=self.tmp.name; S.CFG=S.load_config(str(Path(S.DATA)/'config.json'))
        S.CONTENT=S.bundled_content(); S.DB=S.open_db(str(Path(S.DATA)/'db'))
        self.ws=event_workspace.initialize(S,str(Path(S.DATA)/'db'),S.DATA)
        self.addCleanup(self.restore)
        self.token='synthetic-agent-token-for-test-only-'+'x'*32
        self.grants=Path(S.DATA)/'grants.json'
        self.principal={'id':'test-owner','label':'Test owner','realm':'event','persona':'operator',
            'audience':'kevenos','token_sha256':hashlib.sha256(self.token.encode()).hexdigest(),
            'scopes':['event:read','event:write'],'workspaces':['live','rehearsal'],
            'expires_at':time.time()+3600,'revoked':False}
        self.save_grant()
        env=patch.dict(os.environ,{'KEVENOS_HOSTED':'1','KEVENOS_AGENT_PRINCIPALS_FILE':str(self.grants),
            'KEVENOS_GATEWAY_ENABLED':'0','KEVENOS_SMTP_ENABLED':'0'})
        env.start(); self.addCleanup(env.stop)
    def restore(self):
        self.ws.close()
        for k,v in self.before.items():setattr(S,k,v)
    def save_grant(self):self.grants.write_text(json.dumps({'principals':[self.principal]}))
    def req(self,path,body=None,token=True,method=None):
        return access.request(method or ('POST' if body is not None else 'GET'),'/api/v1/agent/'+path,body,
            headers={'Authorization':'Bearer '+self.token} if token else {},auto_context=False)[:2]
    def test_machine_api_requires_bearer_not_browser_cookie(self):
        code,value=self.req('status',token=False)
        self.assertEqual(code,401,value)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'],'unauthorized')
    def test_status_and_capabilities_are_scoped(self):
        code,value=self.req('status');self.assertEqual(code,200,value)
        self.assertEqual(value['data']['principal']['id'],'test-owner')
        self.assertTrue(value['context']);self.assertTrue(value['revision'])
        self.assertEqual(value['workspace'],'live')
        code,value=self.req('capabilities');self.assertEqual(code,200,value)
        self.assertTrue(value['data']['resources'])
        self.assertNotIn(self.token,json.dumps(value))

    def test_engagement_agent_commands_require_grant_and_exact_confirmation(self):
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2':'1'}):
            code, result = self.req('capabilities')
            self.assertNotIn('engagement.control', [item['name'] for item in result['data']['commands']])
            code, result = self.req('read', {'resource':'engagement.state','arguments':{}})
            self.assertEqual((code, result['error']['code']), (403, 'scope_forbidden'))
            self.principal['scopes'] += ['engagement:read', 'engagement:write']
            self.save_grant()
            code, result = self.req('capabilities')
            self.assertIn('engagement.control', [item['name'] for item in result['data']['commands']])
            args = {'action':'save','revision':0,'payload':{'title':'Agent poll','kind':'poll','questions':[
                {'type':'single','prompt':'Choose','options':['A','B']}]}}
            body = self.prepared('engagement.control', args)
            code, result = self.req('execute', {k:v for k,v in body.items() if k not in ('confirmation_id','confirm')})
            self.assertEqual((code, result['error']['code']), (409, 'confirmation_required'))
            code, result = self.req('execute', body)
            self.assertEqual(code, 200, result)
            self.assertEqual(result['data']['revision'], 1)
            code, replay = self.req('execute', body)
            self.assertEqual(code, 200, replay)
            self.assertTrue(replay['replayed'])
            self.assertEqual(S.kv_get('engagement_catalog_v1')['activities'][0]['draft']['title'], 'Agent poll')
            read_body = {'resource':'engagement.state','arguments':{'collection':'activities','limit':1}}
            code, page = self.req('read', read_body)
            self.assertEqual(code, 200, page)
            self.assertEqual((page['data']['total'], len(page['data']['activities'])), (1, 1))
            pending = self.prepared('engagement.control', {'action':'save','revision':1,'payload':{**args['payload'],'title':'Next'}})
            import engagement_catalog
            engagement_catalog.execute(S, 'save', {**args['payload'],'title':'Concurrent'}, expected_revision=1,
                request_id='a'*32, identity={'role':'organizer','credential_id':'operator'})
            code, changed = self.req('execute', pending)
            self.assertEqual((code, changed['error']['code']), (409, 'revision_changed'))

    def test_agent_can_preview_and_confirm_copy_of_legacy_draft(self):
        import engagement_catalog
        self.principal['scopes'] += ['engagement:read', 'engagement:write']
        self.save_grant()
        old = {'rev':1,'packs':[{'id':'a'*32,'status':'draft','title':'Old','description':'',
            'questions':[{'id':'b'*32,'type':'text','prompt':'Question?'}]}]}
        with S.DB: engagement_catalog._write(S.DB, 'audience_questions_v1', old)
        with patch.dict(os.environ, {'KEVENOS_ENGAGEMENT_V2':'1'}):
            code, result = self.req('read', {'resource':'engagement.legacy_preview','arguments':{}})
            self.assertEqual(code, 200, result)
            item = result['data']['audience']['drafts'][0]
            args = {'action':'copy_legacy_pack','revision':0,'payload':{'pack_id':item['id'],'source_digest':item['digest']}}
            code, result = self.req('execute', self.prepared('engagement.control', args))
            self.assertEqual(code, 200, result)
            self.assertEqual(S.kv_get('audience_questions_v1'), old)
            self.assertEqual(S.kv_get('engagement_catalog_v1')['activities'][0]['draft']['title'], 'Old')
    def test_revocation_expiry_and_realm_are_fail_closed(self):
        for key,value in [('revoked',True),('expires_at',0),('realm','mars'),('persona','mars'),('audience','another-app')]:
            old=self.principal[key];self.principal[key]=value;self.save_grant()
            code,result=self.req('status');self.assertIn(code,(401,503),(key,result))
            self.principal[key]=old;self.save_grant()
    def test_cookie_is_not_a_machine_grant(self):
        code,_,headers=access.request('POST','/api/access/login',{'role':'admin','password':S.CFG['password']},auto_context=False)
        self.assertEqual(code,200)
        cookie=next(v for v in headers['set-cookie'] if v.startswith('pp_access=')).split(';')[0]
        code,_,_=access.request('GET','/api/v1/agent/status',cookie=cookie,auto_context=False)
        self.assertEqual(code,401)

    def status(self):
        code,value=self.req('status'); self.assertEqual(code,200,value); return value
    def prepared(self,name,args):
        status=self.status()
        b={'command':name,'arguments':args,'context':status['context'],'revision':status['revision']}
        code,result=self.req('prepare',b);self.assertEqual(code,200,result)
        return {**b,'idempotency_key':str(uuid.uuid4()),'confirmation_id':result['data']['confirmation_id'],'confirm':'EXECUTE'}
    def test_confirmation_and_durable_replay(self):
        body=self.prepared('program.announce',{'text':'Testing event announcement'})
        no_confirm={k:v for k,v in body.items() if k not in ('confirmation_id','confirm')}
        code,result=self.req('execute',no_confirm);self.assertEqual((code,result['error']['code']),(409,'confirmation_required'))
        code,result=self.req('execute',body);self.assertEqual(code,200,result)
        written=S.kv_get('program');self.assertEqual(written['announce'],'Testing event announcement')
        # Receipts written before5.14.3 do not contain explicit workspace metadata.
        legacy=dict(result);legacy.pop('workspace')
        S.AUTH_DB.execute('UPDATE agent_receipts SET response=? WHERE key=?',(json.dumps(legacy),body['idempotency_key']));S.AUTH_DB.commit()
        code,replay=self.req('execute',body);self.assertEqual(code,200,replay);self.assertTrue(replay['replayed'])
        self.assertEqual(replay['workspace'],'live')
        self.assertEqual(S.kv_get('program'),written)
        self.assertEqual(S.DB.execute("SELECT count(*) FROM audit WHERE action='program-announce'").fetchone()[0],1)
        changed=dict(body,arguments={'text':'A different event announcement'})
        code,result=self.req('execute',changed);self.assertEqual((code,result['error']['code']),(409,'idempotency_conflict'))
        code,receipt=self.req('receipts',{'idempotency_key':body['idempotency_key']})
        self.assertEqual(code,200);self.assertEqual(receipt['data']['state'],'completed')
    def test_stale_browser_update_refuses_agent_write(self):
        body=self.prepared('program.announce',{'text':'Old agent proposal'})
        S.kv_set('program',{'i':0,'mode':'manual','since':time.time(),'announce':'Newer desk announcement'})
        code,result=self.req('execute',body);self.assertEqual((code,result['error']['code']),(409,'revision_changed'))
        self.assertEqual(S.kv_get('program')['announce'],'Newer desk announcement')
    def test_unknown_fields_and_scope_refused_before_writes(self):
        body=self.prepared('program.announce',{'text':'Allowed'})
        body['arguments']['password']='ShouldNeverAppearInErrors'
        code,result=self.req('execute',body);self.assertEqual(code,400,result)
        self.assertNotIn('ShouldNeverAppearInErrors',json.dumps(result))
        code,result=self.req('read',{'resource':'scoring.sheets','arguments':{}})
        self.assertEqual((code,result['error']['code']),(403,'scope_forbidden'))
        self.assertEqual(S.DB.execute('SELECT count(*) FROM agent_receipts').fetchone()[0],0)
    def test_failure_after_mutation_is_never_reexecuted(self):
        import event_commands
        body=self.prepared('program.announce',{'text':'Ambiguous'})
        def crash(*args):
            S.kv_set('program',{'i':0,'mode':'manual','announce':'Mutation happened','since':time.time()})
            raise RuntimeError('Internal failure must be hidden')
        with patch.object(event_commands,'execute',side_effect=crash) as mocked:
            code,result=self.req('execute',body);self.assertEqual((code,result['error']['code']),(500,'outcome_uncertain'))
            code,result=self.req('execute',body);self.assertEqual((code,result['error']['code']),(409,'outcome_uncertain'))
            self.assertEqual(mocked.call_count,1)
        self.assertNotIn('Internal failure',json.dumps(result))
    def test_workspace_scope_and_changed_context(self):
        body=self.prepared('program.announce',{'text':'Wrong workspace'})
        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        code,result=self.req('execute',body);self.assertEqual((code,result['error']['code']),(409,'workspace_changed'))
        self.assertEqual(result['workspace'],'rehearsal')
        self.principal['workspaces']=['live'];self.save_grant()
        code,result=self.req('status');self.assertEqual((code,result['error']['code']),(403,'workspace_forbidden'))
    def test_expired_plan_and_revoked_replay(self):
        body=self.prepared('program.announce',{'text':'Expires'})
        S.AUTH_DB.execute('UPDATE agent_confirmations SET expires=0');S.AUTH_DB.commit()
        code,result=self.req('execute',body);self.assertEqual((code,result['error']['code']),(409,'confirmation_required'))
        body=self.prepared('program.announce',{'text':'Executed once'})
        self.assertEqual(self.req('execute',body)[0],200)
        self.principal['revoked']=True;self.save_grant();self.assertEqual(self.req('execute',body)[0],401)
    def test_no_privilege_crossover_to_browser_or_other_principal(self):
        code,_,_=access.request('GET','/api/desk/state',headers={'Authorization':'Bearer '+self.token},auto_context=False)
        self.assertEqual(code,401)
        body=self.prepared('program.announce',{'text':'Owner one'})
        self.assertEqual(self.req('execute',body)[0],200)
        self.principal['id']='other-owner';self.save_grant()
        code,result=self.req('receipts',{'idempotency_key':body['idempotency_key']})
        self.assertEqual((code,result['error']['code']),(404,'receipt_not_found'))
        self.assertEqual(self.req('execute',body)[0],409)
    def test_openapi_has_only_authorized_commands(self):
        code,result=self.req('openapi.json');self.assertEqual(code,200,result)
        self.assertEqual(result['data']['openapi'],'3.1.0')
        self.assertIn('program.announce',json.dumps(result))
        self.assertNotIn('scoring.correct_sheet',json.dumps(result))

    def test_quiz_read_and_control_follow_scopes_and_shared_desk_state(self):
        self.principal['scopes']=['scoring:read']
        self.save_grant()
        code,result=self.req('capabilities')
        self.assertEqual(code,200,result)
        self.assertIn('quiz.state',[row['name'] for row in result['data']['resources']])
        self.assertIn('quiz.analytics',[row['name'] for row in result['data']['resources']])
        self.assertNotIn('quiz.control',[row['name'] for row in result['data']['commands']])

        code,result=self.req('read',{'resource':'quiz.state','arguments':{'include_bank':True}})
        self.assertEqual(code,200,result)
        self.assertFalse(result['data']['enabled'])
        self.assertIn('PQ1',[row['id'] for row in result['data']['bank']])
        self.principal['scopes'].append('scoring:write')
        self.save_grant()
        body=self.prepared('quiz.control',{'action':'settings','enabled':True})
        unconfirmed={key:value for key,value in body.items() if key not in {'confirmation_id','confirm'}}
        self.assertEqual(self.req('execute',unconfirmed)[0],409)
        code,result=self.req('execute',body)
        self.assertEqual(code,200,result)
        self.assertTrue(result['data']['enabled'])
        self.assertEqual(self.req('execute',body)[1]['replayed'],True)

        read={'resource':'quiz.analytics','arguments':{}}
        code,result=self.req('read',read)
        self.assertEqual(code,200,result)
        self.assertEqual(result['data']['activity'][0]['action'],'settings')
        self.assertNotIn('actor',result['data']['activity'][0])
        self.assertEqual(result['data']['activity'][0]['detail'],'Enabled')
        self.assertEqual(result['data']['summary']['released'],0)

        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        code,result=self.req('read',read)
        self.assertEqual(code,200,result)
        self.assertFalse(result['data']['enabled'])
        self.assertEqual(result['data']['activity'],[])

    def test_quiz_control_and_browser_share_revisions_analytics_and_rules(self):
        self.principal['scopes']=['scoring:read','scoring:write']
        self.save_grant()

        def execute(args, status=200):
            code,result=self.req('execute',self.prepared('quiz.control',args))
            self.assertEqual(code,status,result)
            return result

        def login(role,password):
            code,result,headers=access.request('POST','/api/access/login',{'role':role,'password':password})
            self.assertEqual(code,200,result)
            return next(value for value in headers['set-cookie'] if value.startswith('pp_access=')).split(';')[0]

        def browser(path, data=None, cookie=None):
            code,result,_=access.request('POST' if data is not None else 'GET',path,data,
                                          cookie=cookie or admin,headers={'X-Event-Context':self.ws.context})
            self.assertEqual(code,200,result)
            return result

        admin=login('admin',S.CFG['password'])
        browser('/api/desk/roster',{'rows':[{'pair':1,'partner':'Example Company','p1':'Synthetic Participant Name'}]})
        credential=browser('/api/admin/credentials',{'action':'create','role':'participant','label':'Synthetic label','pair':1,'member':1})
        participant=login('participant',credential['password'])
        execute({'action':'settings','enabled':True})
        browser('/api/participant/quiz',{'action':'join'},cookie=participant)

        old=self.prepared('quiz.control',{'action':'send','qid':'W2'})
        desk=browser('/api/desk/quiz')
        browser('/api/desk/quiz',{'action':'edit','rev':desk['rev'],'qid':'W2','changes':{'prompt':'Synthetic timing question'}})
        code,result=self.req('execute',old)
        self.assertEqual((code,result['error']['code']),(409,'revision_changed'))
        run_id,=execute({'action':'send','qid':'W2'})['data']['new_run_ids']
        current=self.prepared('quiz.control',{'action':'close','id':run_id})
        run,=browser('/api/participant/quiz',cookie=participant)['questions']
        self.assertEqual((run['id'],run['prompt'],run['end']-run['start']),(run_id,'Synthetic timing question',60))
        browser('/api/participant/quiz',{'action':'answer','id':run_id,'key':'B'},cookie=participant)
        code,result=self.req('execute',current)
        self.assertEqual(code,200,result)

        code,result=self.req('read',{'resource':'quiz.analytics','arguments':{'run_id':run_id,'status':'closed','query':'timing question'}})
        self.assertEqual(code,200,result)
        analytics=result['data']
        question,=analytics['questions']
        self.assertEqual((question['id'],question['responses'],question['completion']),(run_id,1,100))
        self.assertEqual(analytics['activity'][0]['action'],'close')
        self.assertNotIn('Synthetic Participant Name',json.dumps(analytics))
        self.assertNotIn(credential['password'],json.dumps(analytics))

    def test_quiz_cue_preview_is_read_only_and_reset_receipt_is_replayed(self):
        self.principal['scopes']=['scoring:read','scoring:write']
        self.save_grant()
        before = self.status()
        args = {'qid':'W1','time':'10:04','shift_following':True}
        code,result = self.req('read',{'resource':'quiz.timeline_preview','arguments':args})
        self.assertEqual(code,200,result)
        self.assertEqual(result['revision'],before['revision'])
        self.assertEqual([row['after']['offset_minutes'] for row in result['data']['changes']],[4,9])
        code,result = self.req('execute',self.prepared('quiz.control',{'action':'retime',**args}))
        self.assertEqual(code,200,result)
        self.assertEqual(result['data']['new_run_ids'],[])
        reset = self.prepared('quiz.control',{'action':'reset','confirm':'RESET QUIZ','reason':'New practice round'})
        no_confirm = {key:value for key,value in reset.items() if key not in ('confirmation_id','confirm')}
        self.assertEqual(self.req('execute',no_confirm)[0],409)
        code,result = self.req('execute',reset)
        self.assertEqual(code,200,result)
        archive_id = result['data']['archive_id']
        self.assertTrue(self.req('execute',reset)[1]['replayed'])
        _,view = self.req('read',{'resource':'quiz.state','arguments':{}})
        self.assertEqual([a['id'] for a in view['data']['reset_archives']],[archive_id])
        self.assertEqual(view['data']['restorable_reset_id'],archive_id)
        restore = self.prepared('quiz.control',{'action':'restore_reset','archive_id':archive_id,
                                               'confirm':'RESTORE QUIZ','reason':'Recover practice round'})
        code,result = self.req('execute',restore)
        self.assertEqual(code,200,result)
        self.assertFalse(result['data']['enabled'])

    def test_receipt_access_follows_current_scope_and_original_workspace(self):
        body=self.prepared('program.announce',{'text':'Sensitive live outcome'})
        self.assertEqual(self.req('execute',body)[0],200)
        self.principal['scopes']=['event:read'];self.save_grant()
        code,result=self.req('receipts',{'idempotency_key':body['idempotency_key']})
        self.assertEqual((code,result['error']['code']),(403,'scope_forbidden'))
        self.principal['scopes']=['event:read','event:write'];self.principal['workspaces']=['rehearsal'];self.save_grant()
        S.WORKSPACE.switch('rehearsal','REHEARSAL')
        code,result=self.req('receipts',{'idempotency_key':body['idempotency_key']})
        self.assertEqual((code,result['error']['code']),(403,'workspace_forbidden'))
    def test_token_revoked_between_upload_and_guard_cannot_execute(self):
        import agent_api
        body=self.prepared('program.announce',{'text':'Must not run'})
        original=agent_api.authenticate
        count=0
        def raced(headers):
            nonlocal count
            ident=original(headers); count+=1
            if count==1:
                self.principal['revoked']=True;self.save_grant()
            return ident
        with patch.object(agent_api,'authenticate',side_effect=raced):
            code,result=self.req('execute',body)
        self.assertEqual(code,401,result)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM agent_receipts').fetchone()[0],0)

    def test_write_only_grant_cannot_disclose_readback(self):
        self.principal['scopes']=['event:read','attendees:write'];self.save_grant()
        meta=self.status()
        code,result=self.req('prepare',{'command':'attendees.update_participant','arguments':{'pair':1,'member':1,'checked_in':True},'context':meta['context'],'revision':meta['revision']})
        self.assertEqual((code,result['error']['code']),(403,'scope_forbidden'))
        code,catalog=self.req('capabilities');self.assertEqual(code,200)
        self.assertNotIn('attendees.update_participant',[e['name'] for e in catalog['data']['commands']])

    def test_live_preparation_requires_both_workspaces_before_read_or_prepare(self):
        self.principal['scopes']=['event:read','event:write','workspace:write','access:read','access:write']
        self.principal['workspaces']=['live'];self.save_grant()
        code,result=self.req('read',{'resource':'workspace.live_preview','arguments':{}})
        self.assertEqual((code,result['error']['code']),(403,'workspace_forbidden'))
        current=self.status()
        body={'command':'workspace.prepare_live','arguments':{'plan_digest':'a'*64,'guest_policy':'revoke','confirm':'PREPARE LIVE'},'context':current['context'],'revision':current['revision']}
        code,result=self.req('prepare',body)
        self.assertEqual((code,result['error']['code']),(403,'workspace_forbidden'))

    def test_recovery_barrier_returns_503_without_event_metadata(self):
        self.ws.guard.blocked=True
        try:
            code,result=self.req('status')
            self.assertEqual(code,503)
            self.assertIn('recovery',str(result['error']).lower())
            self.assertNotIn('revision',result)
        finally:self.ws.guard.blocked=False

    def test_transition_catalog_uses_existing_access_scopes(self):
        self.principal['scopes']=['event:read','event:write','workspace:write','access:read','access:write'];self.save_grant()
        code,result=self.req('capabilities');self.assertEqual(code,200)
        self.assertIn('workspace.live_preview',{e['name'] for e in result['data']['resources']})
        self.assertIn('workspace.prepare_live',{e['name'] for e in result['data']['commands']})
        self.principal['scopes'].remove('access:write');self.save_grant()
        code,result=self.req('capabilities')
        self.assertNotIn('workspace.prepare_live',{e['name'] for e in result['data']['commands']})
