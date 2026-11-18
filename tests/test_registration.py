import unittest
import base64
from urllib.parse import urlsplit

import test_workspace_routes as workspace

S = workspace.S
PATH = '/api/registration'
REDEEM = '/api/access/registration-link'


class Registration(unittest.TestCase):
    def setUp(self):
        self.f = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.desk = self.f.login(self.f.issue('registration'))

    def snapshot(self):
        return self.f.get(PATH, cookie=self.desk)[0]

    def command(self, action, status=200, **kwargs):
        return self.f.post(PATH, {'action': action, 'revision': self.snapshot()['revision'], **kwargs},
                           cookie=self.desk, status=status)[0]

    def test_walk_in_and_move_preserves_checkin_but_revokes_old_access(self):
        self.command('register', pair=1, member=2, name='Walk-in Example', checked_in=True)
        issued = self.command('issue', pair=1, member=2)
        old = self.f.login(issued['password'])
        self.command('move', pair=1, member=2, to_pair=2, to_member=1, company='Example Partner')
        self.f.get('/api/participant/state', cookie=old, status=401)
        team = next(row for row in self.snapshot()['teams'] if row['pair'] == 2)
        self.assertTrue(team['members'][0]['checked_in'])
        self.assertEqual(team['members'][0]['name'], 'Walk-in Example')
        self.assertFalse(team['members'][0]['has_password'])

    def test_qr_uses_one_time_token_and_preserves_existing_password(self):
        issued = self.command('issue', pair=1, member=1)
        qr = self.command('qr', pair=1, member=1)
        self.assertNotIn(issued['password'], qr['link'])
        self.assertIn(b'<svg', base64.b64decode(qr['qr_image'].split(',', 1)[1]))
        code = urlsplit(qr['link']).fragment
        _, headers = self.f.post(REDEEM, {'code': code}, context=None)
        cookie = next(item.split(';')[0] for item in headers['set-cookie'] if item.startswith('pp_access='))
        data, _ = self.f.get('/api/participant/state', cookie=cookie)
        self.assertEqual(data['participant']['pair'], 1)
        self.f.post(REDEEM, {'code': code}, context=None, status=403)
        self.assertTrue(self.f.login(issued['password']))
        second = self.command('qr', pair=1, member=1)
        self.command('reset', pair=1, member=1)
        self.f.post(REDEEM, {'code': urlsplit(second['link']).fragment}, context=None, status=403)

    def test_full_team_swap_preserves_each_checkin(self):
        self.command('register', pair=2, member=1, name='Second Example', checked_in=True)
        self.command('swap', pair=1, member=1, to_pair=2, to_member=1)
        first, second = self.snapshot()['teams'][:2]
        self.assertEqual(first['members'][0]['name'], 'Second Example')
        self.assertTrue(first['members'][0]['checked_in'])
        self.assertEqual(second['members'][0]['name'], 'Live Test Person')
        self.assertFalse(second['members'][0]['checked_in'])

    def test_qr_rejects_expiry_and_workspace_switch(self):
        qr = self.command('qr', pair=1, member=1)
        with S.AUTH_DB:
            S.AUTH_DB.execute('UPDATE registration_links SET expires=0')
        self.f.post(REDEEM, {'code': urlsplit(qr['link']).fragment}, context=None, status=403)
        qr = self.command('qr', pair=1, member=1)
        self.f.switch('rehearsal')
        self.f.post(REDEEM, {'code': urlsplit(qr['link']).fragment}, context=None, status=403)

    def test_qr_opens_participant_from_an_existing_guest_session(self):
        guest = self.f.login(S.CFG['guest_password'])
        qr = self.command('qr', pair=1, member=1)
        code = urlsplit(qr['link']).fragment
        result, headers = self.f.post(REDEEM, {'code': code}, cookie=guest,
            headers={'Origin': 'https://event.example.com', 'Sec-Fetch-Site': 'same-origin',
                     'X-Access-Context': ''}, context=None)
        self.assertEqual(result['next'], '/participant')
        cookie = next(item.split(';')[0] for item in headers['set-cookie'] if item.startswith('pp_access='))
        data, _ = self.f.get('/api/participant/state', cookie=cookie)
        self.assertEqual(data['participant']['pair'], 1)
        self.assertFalse(self.f.get('/api/access/me', cookie=guest)[0]['authenticated'])
        self.f.post(REDEEM, {'code': code}, context=None, status=403)

    def test_qr_preserves_staff_session_and_keeps_link_usable(self):
        qr = self.command('qr', pair=1, member=1)
        code = urlsplit(qr['link']).fragment
        self.f.post(REDEEM, {'code': code}, cookie=self.desk, headers={'X-Access-Context': ''},
                    context=None, status=409)
        self.f.get(PATH, cookie=self.desk)
        self.f.post(REDEEM, {'code': code}, context=None)

    def test_qr_cannot_replace_a_different_participant(self):
        self.command('register', pair=2, member=1, name='Second Example')
        other = self.f.login(self.command('issue', pair=2, member=1)['password'])
        qr = self.command('qr', pair=1, member=1)
        code = urlsplit(qr['link']).fragment
        self.f.post(REDEEM, {'code': code}, cookie=other, context=None, status=409)
        self.assertEqual(self.f.get('/api/access/me', cookie=other)[0]['pair'], 2)
        self.f.post(REDEEM, {'code': code}, context=None)

    def test_registration_role_cannot_manage_scoring_or_accounts(self):
        self.f.get('/api/desk/scores', cookie=self.desk, status=403)
        self.f.get('/api/admin/credentials', cookie=self.desk, status=403)
        self.f.get('/api/event', cookie=self.desk, status=403)
        self.f.post('/api/desk/clock', {'action':'start'}, cookie=self.desk, status=403)
        participant = self.f.login(self.f.issue('participant', pair=1, member=1))
        self.f.get(PATH, cookie=participant, status=403)
        self.f.post(PATH, {'action':'issue','pair':1,'member':1}, cookie=participant, status=403)

    def test_registration_password_is_created_live_and_revealable_in_rehearsal(self):
        credentials = '/api/admin/credentials'
        issued, _ = self.f.post(credentials, {'action': 'create', 'role': 'registration',
                                             'label': 'Rehearsal Registration Desk'}, cookie=self.f.admin)
        cid = issued['credential']['id']
        self.f.switch('rehearsal')
        self.f.post(credentials, {'action':'create','role':'registration','label':'Rejected shared change'},cookie=self.f.admin,status=409)
        desk = self.f.login(issued['password'])
        reveal = {'action': 'reveal', 'id': cid}
        self.f.post(credentials, reveal, cookie=desk, status=403)
        self.f.post(credentials, reveal, status=401)
        current, headers = self.f.post(credentials, reveal, cookie=self.f.admin)
        self.assertEqual(current['password'], issued['password'])
        self.assertIn('no-store', headers['cache-control'])
        self.assertNotIn(issued['password'], str(self.f.get(credentials, cookie=self.f.admin)[0]))
        self.assertNotIn(issued['password'], str(tuple(S.AUTH_DB.execute(
            'SELECT * FROM access_credentials WHERE id=?', (cid,)).fetchone())))
        self.f.get(PATH, cookie=desk)
        self.f.switch('live')
        self.f.get(PATH, cookie=desk)
        self.f.post(credentials, {'action': 'revoke', 'id': cid}, cookie=self.f.admin)
        self.f.post(credentials, reveal, cookie=self.f.admin, status=404)

    def test_successful_login_makes_hash_only_judge_password_revealable(self):
        credentials = '/api/admin/credentials'
        password = self.f.issue('judge', station=1)
        with S.AUTH_DB:
            row = S.AUTH_DB.execute("SELECT id,revision,password_hash FROM access_credentials WHERE role='judge'").fetchone()
            S.AUTH_DB.execute('UPDATE access_credentials SET password_box=NULL WHERE id=?', (row['id'],))
        reveal = {'action': 'reveal', 'id': row['id']}
        self.f.post(credentials, reveal, cookie=self.f.admin, status=409)
        judge = self.f.login(password)
        self.assertEqual(self.f.post(credentials, reveal, cookie=self.f.admin)[0]['password'], password)
        saved = S.AUTH_DB.execute('SELECT id,revision,password_hash FROM access_credentials WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(tuple(saved), tuple(row))
        self.assertEqual(self.f.get('/api/access/me', cookie=judge)[0]['station'], 1)
        self.f.switch('rehearsal')
        self.f.post(credentials, reveal, cookie=self.f.admin, status=404)
