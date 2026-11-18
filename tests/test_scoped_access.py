"""Exercise hosted authorisation through the real HTTP handler with synthetic data."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_api as T
import test_event_access as A

S = T.S


class ScopedAccess(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = S.CFG, S.DB, S.DATA, S.CONTENT
        self.addCleanup(self.restore)
        S.DATA = self.tmp.name
        S.CFG = S.load_config(str(Path(S.DATA) / "config.json"))
        S.DB = S.open_db(str(Path(S.DATA) / "test.db"))
        S.CONTENT = S.bundled_content()
        S.FAILS.clear()
        S.LIVE.clear()
        env = patch.dict(os.environ, {"KEVENOS_HOSTED": "1", "KEVENOS_GATEWAY_ENABLED": "0"})
        env.start()
        self.addCleanup(env.stop)
        _, self.admin = self.login(S.CFG['password'])

    def restore(self):
        S.DB.close()
        S.CFG, S.DB, S.DATA, S.CONTENT = self.previous

    def call(self, method, path, body=None, cookie='', headers=None):
        code, reply, _ = A.request(method, path, body, cookie, headers)
        return code, reply

    def login(self, password, **fields):
        status, reply, headers = A.request('POST', '/api/access/login',
                                          {'role': A.role_for(password), 'password': password, **fields})
        self.assertEqual(status, 200, reply)
        cookie = next(value for value in headers['set-cookie'] if value.startswith('pp_access=')).split(';', 1)[0]
        return reply, cookie

    def issue(self, station=1, judge="Example Judge"):
        c, reply = self.call('POST', '/api/admin/credentials',
                             {'action': 'create', 'role': 'judge', 'station': station, 'label': judge}, self.admin)
        self.assertEqual(c, 200, reply)
        return reply

    def test_judge_password_has_server_bound_station_role_and_revocation(self):
        self.assertEqual(self.call('POST', '/api/login', {'role': 'judge', 'station': 1, 'name': 'Example Judge'})[0], 410)
        issued = self.issue()
        self.assertEqual(self.call('POST', '/api/access/login', {'role': 'desk', 'password': issued['password']})[0], 403)
        reply, judge = self.login(issued['password'], station=2, name='Wrong name')
        self.assertEqual(reply['station'], 1)
        self.assertEqual(reply['label'], 'Example Judge')
        self.assertEqual(self.call('GET', '/api/state', cookie=judge)[0], 200)
        self.assertEqual(self.call('GET', '/api/desk/state', cookie=judge)[0], 403)
        self.assertEqual(self.call('POST', '/api/sync', {'judge': 'Wrong name'}, judge)[0], 200)
        self.assertEqual(S.LIVE[1]["judge"], "Example Judge")
        self.assertEqual(self.call('POST', '/api/admin/credentials', {'action': 'revoke', 'id': issued['credential']['id']}, self.admin)[0], 200)
        self.assertEqual(self.call('GET', '/api/state', cookie=judge)[0], 401)

    def test_judge_session_expires_reset_reauthenticates_and_replacement_separates_drafts(self):
        issued = self.issue()
        original, original_cookie = self.login(issued['password'])
        with patch.object(S.time, 'time', return_value=S.time.time() + S.JUDGE_TTL + 1):
            self.assertEqual(self.call('GET', '/api/state', cookie=original_cookie)[0], 401)
        status, reset = self.call('POST', '/api/admin/credentials', {'action': 'reset', 'id': issued['credential']['id']}, self.admin)
        self.assertEqual(status, 200, reset)
        reissued, reissued_cookie = self.login(reset['password'])
        self.assertNotEqual(original['draft_key'], reissued['draft_key'])
        self.assertNotEqual(original_cookie, reissued_cookie)
        self.assertEqual(self.call('GET', '/api/state', cookie=original_cookie)[0], 401)
        self.assertEqual(self.call('POST', '/api/access/login', {'role': 'judge', 'password': issued['password']})[0], 403)
        self.call('POST', '/api/admin/credentials', {'action': 'revoke', 'id': issued['credential']['id']}, self.admin)
        replacement, _ = self.login(self.issue(judge='Another Judge')['password'])
        self.assertNotEqual(replacement['draft_key'], reissued['draft_key'])

    def test_organiser_session_expires_and_can_be_revoked(self):
        self.assertEqual(T.call("GET", "/api/desk/state", token=S.token_for("desk"))[0], 401)
        _, issued = self.call('POST', '/api/admin/credentials', {'action': 'create', 'role': 'desk', 'label': 'Event organizer'}, self.admin)
        _, organizer = self.login(issued['password'])
        self.assertEqual(self.call('GET', '/api/desk/state', cookie=organizer)[0], 200)
        with patch.object(S.time, 'time', return_value=S.time.time() + S.ORGANISER_TTL + 1):
            self.assertEqual(self.call('GET', '/api/desk/state', cookie=organizer)[0], 401)
        self.assertEqual(self.call('POST', '/api/access/logout', {}, organizer)[0], 200)
        self.assertEqual(self.call('GET', '/api/desk/state', cookie=organizer)[0], 401)

    def test_retired_hosted_login_paths_do_not_create_sessions(self):
        before = S.DB.execute('SELECT count(*) FROM role_sessions').fetchone()[0]
        for path, body in [('/api/login', {'role': 'desk', 'pin': S.CFG['desk_pin']}),
                           ('/api/site-login', {'password': S.CFG['password']}), ('/api/role-logout', {})]:
            status, reply = self.call('POST', path, body, self.admin)
            self.assertEqual(status, 410, reply)
            self.assertEqual(reply['code'], 'unified_signin')
        self.assertEqual(S.DB.execute('SELECT count(*) FROM role_sessions').fetchone()[0], before)

    def test_names_free_key_never_returns_roster_names(self):
        self.call("POST", "/api/desk/roster", {"rows": [{"pair": 1, "partner": "Private Company", "p1": "Private Person"}]}, self.admin)
        _, judge = self.login(self.issue()['password'])
        status, _ = self.call("POST", "/api/submit", {"pair": 1, "kind": "round", "ticks": [1], "wrap_rating": 3}, judge)
        self.assertEqual(status, 200)
        self.call("POST", "/api/desk/reveal", {"mode": "top3"}, self.admin)
        self.call("POST", "/api/desk/final", {"action": "set", "pairs": [1, 2, 3]}, self.admin)
        self.call("POST", "/api/desk/final", {"action": "start"}, self.admin)
        _, desk = self.call("GET", "/api/desk/state", cookie=self.admin)
        key = desk.get("anonymous_view_key")
        self.assertTrue(key)
        for site in (False, True):
            code, data = T.call("GET", "/api/public?names=1", site=site, extra={"X-Key": key})
            self.assertEqual(code, 200)
            self.assertEqual(data["final"]["pairs"][0]["pair"], 1)
            self.assertNotIn("Private", json.dumps(data))
        code, board = T.call("GET", "/api/board?names=1", site=False, extra={"X-Key": key})
        self.assertEqual(code, 200)
        self.assertNotIn("podium", board)
        self.assertEqual(board["final"]["pairs"][0]["partner"], "Team 1")
        self.assertNotIn("Private", json.dumps(board))
        code, named = T.call("GET", "/api/public", site=False, extra={"X-Key": S.CFG["view_key"]})
        self.assertEqual(code, 200)
        self.assertEqual(named["final"]["pairs"][0]["p1"], "Private Person")
        code, info = T.call("GET", "/api/info", site=False, extra={"X-Key": key})
        self.assertEqual(code, 200)
        self.assertNotIn("view_key", info)


if __name__ == "__main__":
    unittest.main()
