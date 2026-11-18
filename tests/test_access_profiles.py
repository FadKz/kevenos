"""Password roles, profile capabilities and stale browser identity boundaries."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_event_access as access
import event_workspace

S = access.S


class AccessProfiles(unittest.TestCase):
    def setUp(self):
        previous = {key: getattr(S, key) for key in ('CFG', 'DB', 'DATA', 'CONTENT', 'AUTH_DB', 'WORKSPACE')}
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        S.DATA = tmp.name
        S.CFG = S.load_config(str(Path(tmp.name) / 'config.json'))
        S.CFG['guest_password'] = 'SyntheticGuestPassword9'
        S.DB = S.open_db(str(Path(tmp.name) / 'db'))
        S.CONTENT = S.bundled_content()
        S.FAILS.clear()
        controller = event_workspace.initialize(S, str(Path(tmp.name) / 'db'), tmp.name)
        def restore():
            controller.close()
            for key, value in previous.items():
                setattr(S, key, value)
        self.addCleanup(restore)
        env = patch.dict(os.environ, {'KEVENOS_HOSTED': '1', 'KEVENOS_GATEWAY_ENABLED': '0', 'KEVENOS_SMTP_ENABLED': '0'})
        env.start()
        self.addCleanup(env.stop)
        self.cookies = {}
        # The legacy PIN allows fixture setup before the new login is implemented.
        self.admin = self.login('admin', S.CFG['password'], pin=S.CFG['desk_pin'])

    def request(self, method, path, body=None, cookie=None, profile=None, context=True, status=200):
        headers = {}
        if profile:
            headers['X-Profile-Token'] = profile['profile_token']
        if context is True and cookie:
            headers['X-Access-Context'] = profile['context'] if profile else self.cookies.get(cookie, '')
        elif isinstance(context, str):
            headers['X-Access-Context'] = context
        if method == 'POST':
            headers['X-Event-Context'] = S.WORKSPACE.context
        code, result, response_headers = access.request(method, path, body, cookie or '', headers, auto_context=False)
        self.assertEqual(code, status, result)
        return result, response_headers

    def login(self, role, password, **extra):
        result, headers = self.request('POST', '/api/access/login', {'role': role, 'password': password, **extra})
        cookie = next(value for value in headers['set-cookie'] if value.startswith('pp_access=')).split(';')[0]
        self.cookies[cookie] = result.get('context', headers.get('x-access-context', [''])[0])
        return cookie

    def issue(self, role, **fields):
        value, _ = self.request('POST', '/api/admin/credentials',
            {'action': 'create', 'role': role, 'label': 'Synthetic ' + role, **fields}, self.admin)
        return value

    def profile(self, role, mode='preview', **fields):
        value, _ = self.request('POST', '/api/access/impersonate',
            {'role': role, 'mode': mode, **fields}, self.admin)
        return value

    def roster(self):
        self.request('POST', '/api/desk/roster', {'rows': [
            {'pair': 1, 'p1': 'First participant', 'p2': 'Teammate'},
            {'pair': 3, 'p1': 'Other participant'}]}, self.admin)

    def test_profile_stylesheet_is_served(self):
        status, body, headers = access.request('GET', '/static/profile.css', auto_context=False)
        self.assertEqual(status, 200)
        self.assertIn(b'.kevenos-profile', body)

    def test_owner_uses_password_without_pin(self):
        cookie = self.login('admin', S.CFG['password'])
        me, _ = self.request('GET', '/api/access/me', cookie=cookie)
        self.assertEqual(me['role'], 'admin')
        self.assertTrue(me['context'])

    def test_selected_role_must_match_normal_credential(self):
        self.request('POST', '/api/access/login', {'role': 'judge', 'password': S.CFG['guest_password']}, status=403)

    def test_admin_can_request_another_interface_without_impersonating_automatically(self):
        result, _ = self.request('POST', '/api/access/login', {'role': 'participant', 'password': S.CFG['password']})
        self.assertEqual(result['role'], 'admin')
        self.assertEqual(result['next'], '/admin?view=participant#profiles')
        self.assertIsNone(result['impersonation'])

    def test_managed_judge_password_binds_station_and_reset_revokes(self):
        issued = self.issue('judge', station=3)
        self.assertIn('issuance_grant', issued)
        cookie = self.login('judge', issued['password'], station=1)
        me, _ = self.request('GET', '/api/access/me', cookie=cookie)
        self.assertEqual((me['role'], me['station'], me['next']), ('judge', 3, '/judge/3'))
        self.assertTrue(me['draft_key'])
        state, _ = self.request('GET', '/api/state', cookie=cookie)
        self.assertEqual(state['assignment']['pair'], 3)
        self.request('POST', '/api/access/login', {'role':'desk', 'password':issued['password']}, status=403)
        reset, _ = self.request('POST', '/api/admin/credentials', {'action':'reset', 'id':issued['credential']['id']}, self.admin)
        self.request('GET', '/api/state', cookie=cookie, context=False, status=401)
        renewed = self.login('judge', reset['password'])
        new, _ = self.request('GET', '/api/access/me', cookie=renewed)
        # Rotation creates a new authority revision; old outboxes cannot replay.
        self.assertNotEqual(new['draft_key'], me['draft_key'])

    def test_hosted_legacy_paths_and_tokens_cannot_authenticate(self):
        for endpoint in ('/api/login', '/api/site-login', '/api/site-logout', '/api/role-logout'):
            self.request('POST', endpoint, {'password': S.CFG['password'], 'pin': S.CFG['desk_pin'], 'role': 'desk'}, status=410)
        judge = self.login('judge', self.issue('judge', station=1)['password'])
        guest = self.login('guest', S.CFG['guest_password'])
        bearer = judge.split('=', 1)[1]
        code, me, _ = access.request('GET', '/api/access/me', cookie=guest, headers={'X-Token':bearer}, auto_context=False)
        self.assertEqual((code, me['role']), (200, 'guest'))
        code, me, _ = access.request('GET', '/api/access/me', headers={'X-Token':bearer}, auto_context=False)
        self.assertEqual((code, me['authenticated']), (200, False))

    def test_missing_and_stale_markers_are_checked_before_module_writes_and_reads(self):
        self.request('POST', '/api/desk/program', {'action':'announce', 'text':'Must not save'}, self.admin, context=False, status=409)
        guest = self.login('guest', S.CFG['guest_password'])
        self.request('GET', '/api/admin/credentials', cookie=self.admin, context=self.cookies[guest], status=409)
        self.request('POST', '/api/admin/credentials', {'action':'create', 'role':'desk', 'label':'Blocked'}, self.admin,
                     context=self.cookies[guest], status=409)
        self.assertFalse(S.DB.execute("SELECT 1 FROM access_credentials WHERE label='Blocked'").fetchone())

    def test_profiles_cover_unissued_people_and_use_exact_participant_resource_scope(self):
        self.roster()
        profiles, _ = self.request('GET', '/api/admin/profiles', cookie=self.admin)
        self.assertEqual(len([p for p in profiles['targets'] if p['role']=='judge']), S.CFG['stations'])
        self.assertEqual(len([p for p in profiles['targets'] if p['role']=='participant']), 3)
        selected = self.profile('participant', pair=1, member=1)
        self.assertEqual(selected['identity']['actor']['credential_id'], 'owner-recovery')
        self.assertEqual(selected['identity']['impersonation']['mode'], 'preview')
        me, _ = self.request('GET', '/api/access/me', cookie=self.admin, profile=selected)
        self.assertEqual((me['pair'],me['member'],me['label']), (1,1,'First participant'))
        state, _ = self.request('GET', '/api/participant/state?pair=3', cookie=self.admin, profile=selected)
        self.assertEqual(state['participant']['pair'], 1)
        self.assertNotIn('Other participant', json.dumps(state))
        # A sanitized package has no event documents. The scoped profile still
        # cannot turn a missing document into access to another role's data.
        self.request('GET', '/api/resources/participant-pack-s1', cookie=self.admin, profile=selected, status=404)
        self.request('GET', '/api/resources/participant-pack-s2', cookie=self.admin, profile=selected, status=404)
        self.request('GET', '/api/resources/judge-guide', cookie=self.admin, profile=selected, status=404)
        self.request('GET', '/api/admin/credentials', cookie=self.admin, profile=selected, status=403)

    def test_every_hosted_interface_receives_profile_runtime_marker(self):
        for path in ('/participant', '/guest', '/live', '/board', '/admin', '/desk', '/judge/1', '/'):
            page, _ = self.request('GET', path, cookie=self.admin)
            self.assertTrue(b'data-scoped="1"' in page, path)

    def test_preview_denies_mutations_and_sync_does_not_update_station(self):
        preview = self.profile('judge', station=1)
        before = dict(S.LIVE)
        self.request('POST', '/api/sync', {'ticks':[1]*10}, self.admin, profile=preview)
        self.assertEqual(S.LIVE, before)
        for path, body in (('/api/submit', {'pair':1}), ('/api/session', {'action':'start'}),
                           ('/api/ai/tidy', {'notes':'private'}), ('/api/admin/notifications', {}),
                           ('/api/desk/clock', {'action':'start'})):
            self.request('POST', path, body, self.admin, profile=preview, status=403)
        self.assertEqual(S.active_sheets('round'), [])

    def test_support_mode_has_role_boundaries_and_attributes_real_admin(self):
        support = self.profile('judge', mode='support', station=1)
        self.request('POST', '/api/submit', {'kind':'round','pair':1,'ticks':[1]*5,'ai':0,'spoke':1,'wrap_rating':3}, self.admin, profile=support)
        record = dict(S.DB.execute("SELECT * FROM audit WHERE action='submit' ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual((record['actor_credential'],record['effective_role'],record['profile_id'],record['impersonation_mode']),
                         ('owner-recovery','judge',support['identity']['impersonation']['id'],'support'))
        activity = dict(S.AUTH_DB.execute("SELECT * FROM visitor_activity WHERE path='/api/submit' ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual((activity['actor_label'],activity['effective_role']), ('Owner','judge'))
        self.request('POST', '/api/desk/program', {'action':'announce','text':'No'}, self.admin, profile=support, status=403)
        self.request('POST', '/api/submit', {'kind':'round','pair':2,'ticks':[]}, self.admin, profile=support, status=409)
        self.assertEqual(len(S.active_sheets('round')), 1)

    def test_mode_change_end_and_parent_binding_fail_closed(self):
        preview = self.profile('judge', station=1)
        support, _ = self.request('POST', '/api/access/impersonate', {'role':'judge','station':1,'mode':'support'}, self.admin, profile=preview)
        self.assertNotEqual(preview['context'], support['context'])
        self.request('GET', '/api/state', cookie=self.admin, profile=preview, status=401)
        other_admin = self.login('admin', S.CFG['password'])
        self.request('GET', '/api/state', cookie=other_admin, profile=support, status=401)
        ended, _ = self.request('POST', '/api/access/impersonate/end', {}, self.admin, profile=support)
        self.assertEqual(ended['context'], self.cookies[self.admin])
        self.request('GET', '/api/state', cookie=self.admin, profile=support, status=401)
        self.request('GET', '/api/admin/profiles', cookie=self.admin)

    def test_profile_expiry_target_reset_and_roster_change_revoke_views(self):
        self.roster()
        issued = self.issue('participant', pair=1, member=1)
        preview = self.profile('participant', credential_id=issued['credential']['id'])
        self.request('POST', '/api/admin/credentials', {'action':'reset','id':issued['credential']['id']}, self.admin)
        self.request('GET', '/api/participant/state', cookie=self.admin, profile=preview, status=401)
        preview = self.profile('participant', pair=3, member=1)
        with patch.object(S.time, 'time', return_value=S.time.time()+901):
            self.request('GET', '/api/participant/state', cookie=self.admin, profile=preview, status=401)
        self.request('POST', '/api/desk/roster', {'rows':[{'pair':1,'p1':'First participant','p2':'Teammate'},
            {'pair':3,'p1':'Changed Person'}]}, self.admin)
        self.request('GET', '/api/participant/state', cookie=self.admin, profile=preview, status=401)

    def test_profile_workspace_scope_and_parent_logout(self):
        profile = self.profile('desk', mode='support')
        self.request('POST', '/api/admin/workspace', {'mode':'rehearsal','confirm':'REHEARSAL'}, self.admin)
        self.request('GET', '/api/desk/state', cookie=self.admin, profile=profile, status=401)
        profile = self.profile('judge', station=1)
        self.request('POST', '/api/access/logout', {}, self.admin, profile=profile)
        self.request('GET', '/api/state', cookie=self.admin, profile=profile, status=401)
        me, _ = self.request('GET', '/api/access/me', cookie=self.admin, context=False)
        self.assertFalse(me['authenticated'])

    def test_forged_target_and_nonadmin_cannot_create_profile(self):
        self.roster()
        issued = self.issue('participant', pair=1, member=1)
        self.request('POST', '/api/access/impersonate', {'role':'judge','credential_id':issued['credential']['id'],'station':1}, self.admin, status=400)
        self.request('POST', '/api/access/impersonate', {'role':'participant','pair':1,'member':99}, self.admin, status=400)
        guest = self.login('guest', S.CFG['guest_password'])
        self.request('POST', '/api/access/impersonate', {'role':'admin'}, guest, status=403)

    def test_schema6_migrates_additively_without_issuing_passwords(self):
        baseline = Path(os.environ.get('KEVENOS_TEST_SCHEMA6_BASE', Path(__file__).resolve().parents[2] / 'base'))
        if not (baseline / 'server.py').exists() or 'APP_VERSION = "4.4.0"' not in (baseline / 'server.py').read_text():
            self.skipTest('Frozen v4.4.0 baseline required for migration proof')
        path = Path(S.DATA) / 'migration.db'
        source = "import server; d=server.open_db(%r); d.execute(\"INSERT INTO roster(pair,partner,p1,p2) VALUES(1,'Old team','Retained person','')\"); d.commit(); d.close()" % str(path)
        subprocess.run([sys.executable, '-c', source], cwd=baseline, check=True, env={**os.environ,'KEVENOS_HOSTED':'0'})
        old = sqlite3.connect(path)
        self.assertNotIn('station', [row[1] for row in old.execute('PRAGMA table_info(access_credentials)')])
        old.close()
        migrated = S.open_db(str(path))
        try:
            self.assertEqual(migrated.execute('SELECT p1 FROM roster WHERE pair=1').fetchone()[0], 'Retained person')
            self.assertIn('station', [row['name'] for row in migrated.execute('PRAGMA table_info(access_credentials)')])
            self.assertEqual(migrated.execute('SELECT COUNT(*) FROM access_credentials').fetchone()[0], 0)
            self.assertEqual(json.loads(migrated.execute("SELECT v FROM kv WHERE k='schema_version'").fetchone()[0]), 8)
            for table in ("agent_receipts", "agent_confirmations", "agent_audit"):
                self.assertTrue(migrated.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        finally:
            migrated.close()


if __name__ == '__main__':
    unittest.main()
