"""Live cutover tests use disposable SQLite workspaces and synthetic accounts."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import event_transition as transition
import test_workspace_routes as fixture
S = fixture.S


class LiveTransition(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.WorkspaceRoutes()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.switch('rehearsal')
        self.judge_passwords = [self.fixture.issue('judge', station=station) for station in range(1, 9)]
        self.controller = S.WORKSPACE

    def apply(self, plan=None, key='test-operation'):
        plan = plan or transition.preview(S)
        return transition.apply(S, plan['plan_digest'], 'PREPARE LIVE', key, 'test-admin')

    def test_preview_and_transition_preserve_staff_and_rehearsal(self):
        participant = self.fixture.issue('participant', pair=1, member=1)
        before = [tuple(r) for r in S.AUTH_DB.execute("SELECT id,password_hash FROM access_credentials WHERE role='judge'")]
        rehearsal_rows = S.DB.execute('SELECT count(*) FROM roster').fetchone()[0]
        connection = S.AUTH_DB
        old_context = self.controller.context
        plan = transition.preview(S)
        self.assertTrue(plan['ready'], plan['blockers'])
        self.assertFalse((self.controller.root / '.live-transition-backups').exists())
        result = self.apply(plan)
        self.assertTrue(result['applied'])
        self.assertIs(connection, S.AUTH_DB)
        self.assertEqual(self.controller.mode, 'live')
        self.assertNotEqual(old_context, self.controller.context)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM roster').fetchone()[0], 0)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM team_assignments').fetchone()[0], 20)
        self.assertEqual(self.controller.rehearsal.execute('SELECT count(*) FROM roster').fetchone()[0], rehearsal_rows)
        self.assertEqual(before, [tuple(r) for r in S.AUTH_DB.execute("SELECT id,password_hash FROM access_credentials WHERE role='judge'")])
        self.assertEqual(S.AUTH_DB.execute("SELECT count(*) FROM account_grants WHERE role='judge' AND dataset='live'").fetchone()[0], 8)
        self.assertEqual(S.AUTH_DB.execute("SELECT count(*) FROM account_grants WHERE role='participant'").fetchone()[0], 0)
        self.assertEqual(S.AUTH_DB.execute("SELECT count(*) FROM access_credentials WHERE role='participant' AND active=1").fetchone()[0], 0)
        self.assertTrue(self.apply(plan)['replayed'])
        for role, password in [('participant',participant),('guest',S.CFG['guest_password'])]:
            code, _, _ = self.fixture.request('POST','/api/access/login',{'role':role,'password':password},context=None)
            self.assertEqual(code,403,role)
        code, _, _ = self.fixture.request('POST','/api/access/login',{'role':'judge','password':self.judge_passwords[0]},context=None)
        self.assertEqual(code,200)
        code, _, _ = self.fixture.request('GET','/api/admin/workspace',cookie=self.fixture.admin)
        self.assertEqual(code,401)

    def test_inactive_live_change_stales_preview(self):
        plan = transition.preview(S)
        with S.AUTH_DB:
            S.AUTH_DB.execute("INSERT INTO kv VALUES('unseen-change','1')")
        with self.assertRaisesRegex(transition.Conflict, 'changed'):
            self.apply(plan)

    def test_publish_failure_restores_content_and_database(self):
        target = self.controller.root / 'content.json'
        old = b'{"old":"sentinel"}'
        target.write_bytes(old)
        plan = transition.preview(S)
        with patch.object(transition, '_after_publish', side_effect=RuntimeError('injected failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.apply(plan)
        self.assertEqual(target.read_bytes(), old)
        self.assertEqual(self.controller.mode, 'rehearsal')
        self.assertIsNone(transition.receipt(S.AUTH_DB))
        self.assertFalse((self.controller.root / transition.JOURNAL).exists())

    def test_mixed_staff_password_and_disabled_state_are_preserved(self):
        self.fixture.issue('participant', pair=1, member=1)
        db = S.AUTH_DB
        participant = dict(db.execute("SELECT * FROM access_credentials WHERE role='participant'").fetchone())
        with db:
            db.execute("INSERT INTO account_grants(account_id,role,dataset) VALUES(?,'registration','global')", (participant['account_id'],))
            db.execute("UPDATE access_credentials SET active=0 WHERE role='guest'")
        self.apply()
        after = dict(db.execute('SELECT * FROM access_credentials WHERE id=?',(participant['id'],)).fetchone())
        self.assertEqual(after['password_hash'], participant['password_hash'])
        self.assertEqual(after['password_box'], participant['password_box'])
        self.assertEqual(after['active'], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM account_grants WHERE account_id=? AND role='registration'",(participant['account_id'],)).fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM account_grants WHERE role='participant'").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM access_credentials WHERE role='guest' AND active=1").fetchone()[0], 0)

    def test_question_definitions_survive_without_answers_or_send_state(self):
        import audience_questions
        import event_quiz
        state = event_quiz.empty_quiz() | {'edits':{'PQ1':{'prompt':'Reviewed question'}},'prize':'Prize', 'answers':{'old':{}},'runs':[{'id':'old'}]}
        pack = audience_questions.build_pack({'title':'Sentiment','questions':[{'type':'text','prompt':'How was it?'}]}, None)
        pack['status'] = 'live'
        pack['closesAt'] = 9999999999999
        S.kv_set(event_quiz.KEY, state)
        S.kv_set(audience_questions.KEY, {'rev':3,'packs':[pack]})
        self.apply()
        new = S.kv_get(event_quiz.KEY)
        self.assertEqual(new['edits'], state['edits'])
        self.assertEqual(new['prize'], 'Prize')
        self.assertEqual(new['answers'], {})
        self.assertEqual(new['runs'], [])
        draft = S.kv_get(audience_questions.KEY)['packs'][0]
        self.assertEqual(draft['status'], 'draft')
        self.assertNotEqual(draft['id'], pack['id'])
        self.assertNotEqual(draft['questions'][0]['id'], pack['questions'][0]['id'])
        self.assertNotIn('closesAt', draft)

    def test_sessions_qr_confirmations_are_revoked_and_receipts_retained(self):
        db = S.AUTH_DB
        with db:
            db.execute("INSERT INTO agent_confirmations VALUES('old','agent','fingerprint','context',9999999999,0)")
            db.execute("INSERT INTO registration_links VALUES('old','credential',1,'rehearsal','old',9999999999)")
        self.assertGreater(db.execute('SELECT count(*) FROM role_sessions').fetchone()[0],0)
        self.apply()
        for table in ('role_sessions','agent_confirmations','registration_links'):
            self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertTrue(transition.receipt(db))

    def test_source_clock_blocks_and_backup_failure_does_not_mutate(self):
        S.kv_set('clock', S._fresh_clock() | {'running':True})
        self.assertFalse(transition.preview(S)['ready'])
        S.kv_set('clock', S._fresh_clock())
        plan = transition.preview(S)
        with patch.object(transition, '_backup', side_effect=OSError('backup unavailable')):
            with self.assertRaises(OSError): self.apply(plan)
        self.assertEqual(S.WORKSPACE.mode, 'rehearsal')
        self.assertIsNone(transition.receipt(S.AUTH_DB))
        self.assertFalse((self.controller.root/transition.JOURNAL).exists())

    def test_committed_failure_finishes_new_content_and_remains_replayable(self):
        plan = transition.preview(S)
        with patch.object(transition, '_after_commit', side_effect=RuntimeError('lost response')):
            with self.assertRaisesRegex(RuntimeError, 'lost response'): self.apply(plan)
        self.assertEqual(S.WORKSPACE.mode, 'live')
        self.assertEqual(S.DB.execute('SELECT count(*) FROM roster').fetchone()[0], 0)
        self.assertTrue(self.apply(plan)['replayed'])
        self.assertFalse((self.controller.root/transition.JOURNAL).exists())

    def test_startup_recovery_uses_commit_marker(self):
        target = self.controller.root/'content.json'
        original = b'{"old":"content"}'
        target.write_bytes(original)
        plan = transition.preview(S)
        with patch.object(transition, '_after_publish', side_effect=RuntimeError('crash')), patch.object(transition, 'recover'):
            with self.assertRaises(RuntimeError): self.apply(plan)
        self.assertNotEqual(target.read_bytes(), original)
        self.assertTrue((self.controller.root/transition.JOURNAL).exists())
        transition.recover(self.controller)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((self.controller.root/transition.JOURNAL).exists())

    def test_corrupt_recovery_fails_closed(self):
        (self.controller.root/transition.JOURNAL).write_text('{"version":99}')
        with self.assertRaisesRegex(RuntimeError, 'recovery is incomplete'):
            transition.recover(self.controller)
        with self.assertRaisesRegex(RuntimeError, 'recovery'):
            with self.controller.read(): pass

    def test_api_command_requires_both_workspaces(self):
        import event_commands
        plan = transition.preview(S)
        with self.assertRaises(event_commands.CommandError) as error:
            event_commands.execute(S, 'workspace.prepare_live', {'plan_digest':plan['plan_digest'], 'confirm':'PREPARE LIVE','guest_policy':'revoke'}, {'id':'test','workspaces':['rehearsal'],'idempotency_key':'command-test'})
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(S.WORKSPACE.mode, 'rehearsal')

    def test_api_execute_finalizes_same_connection_receipt_and_replays(self):
        import agent_api
        principal = {'id':'transition-test','label':'Transition test','workspaces':['live','rehearsal'],
                     'scopes':['workspace:write','access:write','access:read','event:write','event:read']}
        plan = transition.preview(S)
        metadata = agent_api.metadata(S)
        body = {'command':'workspace.prepare_live','arguments':{'plan_digest':plan['plan_digest'],'guest_policy':'revoke','confirm':'PREPARE LIVE'},
                'context':metadata['context'],'revision':metadata['revision']}
        with self.controller.write(), S.LOCK:
            code, prepared = agent_api.operate(S, principal, 'prepare', body, 'test-prepare')
            self.assertEqual(code,200)
            body.update(idempotency_key='4de80844-e380-4a15-95eb-fb83ac973f58',confirmation_id=prepared['data']['confirmation_id'],confirm='EXECUTE')
            code, result = agent_api.operate(S, principal, 'execute', body, 'test-execute')
            self.assertEqual(code,200,result)
            self.assertEqual(result['workspace'],'live')
            saved = S.AUTH_DB.execute("SELECT state FROM agent_receipts WHERE principal=? AND key=?",(principal['id'],body['idempotency_key'])).fetchone()
            self.assertEqual(saved[0],'completed')
            code, replay = agent_api.operate(S,principal,'execute',body,'test-replay')
            self.assertEqual(code,200,replay)
            self.assertEqual(replay['data']['transition_id'],result['data']['transition_id'])

    def test_backup_contains_verified_private_snapshots(self):
        import hashlib
        import sqlite3
        result = self.apply()
        backup = self.controller.root/result['backup_reference']
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        manifest = json.loads((backup/'manifest.json').read_text())
        for name, digest in manifest.items():
            path = backup/name
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
        db = sqlite3.connect(backup/'live.db')
        try:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertGreater(db.execute('SELECT count(*) FROM roster').fetchone()[0],0)
        finally: db.close()

    def test_unknown_schema_blocks(self):
        with S.AUTH_DB:
            S.AUTH_DB.execute("UPDATE kv SET v='99' WHERE k='schema_version'")
        self.assertFalse(transition.preview(S)['ready'])



    def test_mixed_live_staff_password_does_not_occupy_new_participant_slot(self):
        import event_access
        self.controller.switch('live','LIVE')
        with S.AUTH_DB:
            old=event_access.create_credential(S,'participant','Fixture',pair=1,member=1)
            aid=old['credential']['id']
            S.AUTH_DB.execute("INSERT INTO account_grants(account_id,role,dataset) VALUES(?,'registration','global')",(aid,))
        stored=dict(S.AUTH_DB.execute('SELECT * FROM access_credentials WHERE id=?',(aid,)).fetchone())
        self.controller.switch('rehearsal','REHEARSAL');self.apply()
        S.event_config.save_roster(S,[{'pair':1,'p1':'New Live Person'}])
        with S.AUTH_DB:new=event_access.create_credential(S,'participant','New',pair=1,member=1)
        self.assertNotEqual(new['credential']['id'],aid)
        retained=dict(S.AUTH_DB.execute('SELECT * FROM access_credentials WHERE id=?',(aid,)).fetchone())
        for key in ('password_hash','password_box','revision','dataset','active'):
            self.assertEqual(stored[key],retained[key])
        self.assertEqual(retained['role'],'registration')
        self.assertEqual(event_access.reveal_password(S,retained),old['password'])
        code,_,_=self.fixture.request('POST','/api/access/login',{'password':old['password']},context=None)
        self.assertEqual(code,200)

    def test_retained_staff_password_can_be_shared_after_live_transition(self):
        import event_access,event_mail,time
        self.controller.switch('live','LIVE')
        with S.AUTH_DB:
            issued=event_access.create_credential(S,'participant','Fixture',pair=1,member=1)
            aid=issued['credential']['id']
            S.AUTH_DB.execute("INSERT INTO account_grants(account_id,role,dataset) VALUES(?,'registration','global')",(aid,))
        self.controller.switch('rehearsal','REHEARSAL');self.apply()
        with S.AUTH_DB:
            token=event_access.mint_session(S,'admin','owner-recovery','Owner')
        # Use the new real session row, not a fabricated grant/issuer.
        ident=dict(S.AUTH_DB.execute("SELECT * FROM role_sessions WHERE credential_id='owner-recovery'").fetchone())
        result=event_mail.decorate_issuance(S,ident,{'credential':{'id':aid},'password':issued['password']})
        body={'credential_id':aid,'password':issued['password'],'issuance_grant':result['issuance_grant']}
        grant=event_mail._grant(S,ident,body)
        subject,message=event_mail._share_message(S,ident,body,grant)
        self.assertIn('Registration',message)
        with S.AUTH_DB:S.AUTH_DB.execute('UPDATE person_accounts SET active=0 WHERE id=?',(aid,))
        with self.assertRaises(ValueError):event_mail._share_message(S,ident,body,grant)
        with S.AUTH_DB:
            S.AUTH_DB.execute('UPDATE person_accounts SET active=1 WHERE id=?',(aid,))
            S.AUTH_DB.execute("UPDATE account_grants SET dataset='rehearsal' WHERE account_id=?",(aid,))
        with self.assertRaises(ValueError):event_mail._share_message(S,ident,body,grant)
