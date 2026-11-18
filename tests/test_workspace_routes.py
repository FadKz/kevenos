"""Verify dataset isolation at the actual HTTP handler boundary."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_event_access as access
import event_workspace
import event_config

S = access.S
WORKSPACE = "/api/admin/workspace"
CONTEXT = "X-Event-Context"
CURRENT = object()


class WorkspaceRoutes(unittest.TestCase):
    def setUp(self):
        previous = {key: getattr(S, key, None) for key in ("DB", "DATA", "CFG", "CONTENT", "AUTH_DB", "WORKSPACE")}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def restore():
            self.controller.close()
            for key, value in previous.items():
                setattr(S, key, value)

        root = Path(tmp.name)
        S.DATA = tmp.name
        S.CFG = S.load_config(str(root / "config.json"))
        S.CFG["guest_password"] = "SyntheticWorkspaceGuest8!"
        db_path = root / "judging.db"
        S.DB = S.open_db(str(db_path))
        S.FAILS.clear()
        self.controller = event_workspace.initialize(S, str(db_path), S.DATA)
        self.addCleanup(restore)
        env = patch.dict("os.environ", {"KEVENOS_HOSTED": "1", "KEVENOS_GATEWAY_ENABLED": "0", "KEVENOS_SMTP_ENABLED": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.admin = self.login(S.CFG["password"], S.CFG["desk_pin"])
        _, state, headers = self.request("GET", WORKSPACE, cookie=self.admin)
        self.assertEqual(state["mode"], "live")
        self.context = headers[CONTEXT.lower()][0]
        self.staff_passwords = {}
        self.write_roster()

    def request(self, method, path, body=None, cookie="", headers=None, context=CURRENT):
        headers = dict(headers or {})
        if method == "POST" and context is not None:
            marker = getattr(self, "context", None) if context is CURRENT else context
            if marker is not None:
                headers[CONTEXT] = marker
        return access.request(method, path, body, cookie, headers)

    def post(self, path, body, status=200, **kwargs):
        code, result, headers = self.request("POST", path, body, **kwargs)
        self.assertEqual(code, status, result)
        return result, headers

    def get(self, path, status=200, **kwargs):
        code, result, headers = self.request("GET", path, **kwargs)
        self.assertEqual(code, status, result if isinstance(result, dict) else path)
        return result, headers

    def login(self, password, pin=None):
        _, headers = self.post("/api/access/login", {"role": access.role_for(password), "password": password}, context=None)
        return next(cookie for cookie in headers["set-cookie"] if cookie.startswith("pp_access=")).split(";", 1)[0]

    def write_roster(self):
        self.post("/api/desk/roster", {"rows": [{"pair": 1, "partner": "Live Test Company", "p1": "Live Test Person"}]}, cookie=self.admin)

    def issue(self, role, **fields):
        result, _ = self.post("/api/admin/credentials", {
            "action": "create", "role": role, "label": "Synthetic " + role, **fields,
        }, cookie=self.admin)
        return result["password"]

    def staff_password(self, station=1):
        key = (self.controller.mode, station)
        if key not in self.staff_passwords:
            self.staff_passwords[key] = self.issue('judge', station=station, label=f'Synthetic Judge {station}')
        return self.staff_passwords[key]

    def judge(self, password=None, station=1):
        return {'Cookie': self.login(password or self.staff_password(station))}

    def switch(self, mode):
        result, headers = self.post(WORKSPACE, {"mode": mode, "confirm": mode.upper()}, cookie=self.admin)
        self.assertEqual(result["mode"], mode)
        self.assertEqual(result["context"], headers[CONTEXT.lower()][0])
        self.assertNotEqual(result["context"], self.context)
        self.context = result["context"]
        return result

    def test_event_configuration_is_admin_only_and_context_bound(self):
        result, _ = self.get('/api/admin/configuration', cookie=self.admin)
        self.assertEqual(result['configuration']['judge_count'], 8)
        desk = self.login(self.issue('desk'))
        self.get('/api/admin/configuration', status=403, cookie=desk)
        self.post('/api/admin/configuration', {'participant_count': 12, 'team_size': 3, 'judge_count': 4},
                  status=403, cookie=desk)
        self.post('/api/admin/configuration', {'participant_count': 12, 'team_size': True, 'judge_count': 4},
                  status=400, cookie=self.admin)
        self.post('/api/admin/configuration', {'participant_count': 12, 'team_size': 3, 'judge_count': 4},
                  status=409, context='stale-context', cookie=self.admin)
        result, _ = self.post('/api/admin/configuration', {'participant_count': 12, 'team_size': 3, 'judge_count': 4}, cookie=self.admin)
        self.assertEqual((result['configuration']['team_count'], result['configuration']['round_count']), (4, 1))
        self.assertEqual(S.roster_row(1)['members'][0]['name'], 'Live Test Person')

    def test_third_member_keeps_original_scenario_without_packaged_event_files(self):
        self.post('/api/admin/configuration', {'participant_count': 21, 'team_size': 3, 'judge_count': 3}, cookie=self.admin)
        self.post('/api/desk/roster', {'rows': [{'pair': 7, 'partner': 'Example Group', 'members':
                    [{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}, {'id': 3, 'name': 'Three'}]}]}, cookie=self.admin)
        password = self.issue('participant', pair=7, member=3)
        participant = self.login(password)
        event, _ = self.get('/api/event', cookie=participant)
        self.assertEqual((event['participant']['scenario'], event['participant']['station']), ('S4', 1))
        self.assertEqual(event['participant']['teammates'], ['One', 'Two'])
        self.assertEqual(event['resources'], [])
        self.get('/api/resources/participant-pack-s1', status=404, cookie=participant)
        self.get('/api/resources/participant-pack-s4', status=404, cookie=participant)

    def test_rehearsal_configuration_reset_and_switch_preserve_live_settings(self):
        live, _ = self.get('/api/admin/configuration', cookie=self.admin)
        self.switch('rehearsal')
        self.post('/api/admin/configuration', {'participant_count': 11, 'team_size': 3, 'judge_count': 3},
                  status=400, cookie=self.admin)
        result, headers = self.post('/api/admin/configuration', {'participant_count': 11, 'team_size': 3,
                         'judge_count': 3, 'confirm': 'RESET REHEARSAL'}, cookie=self.admin)
        self.context = headers[CONTEXT.lower()][0]
        self.assertEqual(result['summary']['participants'], 11)
        self.assertEqual(result['configuration']['incomplete_team_size'], 2)
        state, _ = self.get(WORKSPACE, cookie=self.admin)
        self.assertEqual((state['summary']['registered_pairs'], state['summary']['round_sheets']), (4, 3))
        result, headers = self.post(WORKSPACE, {'action': 'reset-rehearsal', 'confirm': 'RESET REHEARSAL'}, cookie=self.admin)
        self.context = headers[CONTEXT.lower()][0]
        self.assertEqual(result['summary']['expected_participants'], 11)
        self.switch('live')
        restored, _ = self.get('/api/admin/configuration', cookie=self.admin)
        self.assertEqual(restored['configuration'], live['configuration'])
        self.assertEqual(S.roster_row(1)['p1'], 'Live Test Person')
        self.switch('rehearsal')
        self.assertEqual(event_config.config(S)['team_size'], 3)

    def test_html_and_response_context_and_admin_only_switch(self):
        old = self.context
        desk = self.login(self.issue("desk"))
        guest = self.login(S.CFG["guest_password"])
        for cookie, status in [("", 401), (desk, 403), (guest, 403)]:
            with self.subTest(status=status, auth=bool(cookie)):
                _, headers = self.get(WORKSPACE, status, cookie=cookie)
                self.assertEqual(headers[CONTEXT.lower()], [old])
                _, headers = self.post(WORKSPACE, {"mode": "rehearsal", "confirm": "REHEARSAL"}, status, cookie=cookie)
                self.assertEqual(headers[CONTEXT.lower()], [old])
        self.post(WORKSPACE, {"mode": "rehearsal", "confirm": "LIVE"}, 400, cookie=self.admin)
        self.switch("rehearsal")
        rejected, headers = self.post(WORKSPACE, {"mode": "live", "confirm": "LIVE"}, 409, context=old, cookie=self.admin)
        self.assertEqual(rejected["code"], "workspace_changed")
        self.assertEqual(headers[CONTEXT.lower()], [self.context])
        me, _ = self.get("/api/access/me", cookie=self.admin)
        self.assertEqual(me["role"], "admin")
        for path, cookie in [("/", ""), ("/admin", self.admin), ("/desk", desk), ("/judge/1", "")]:
            with self.subTest(path=path):
                html, headers = self.get(path, cookie=cookie)
                attrs = re.search(rb"<body\b([^>]*)>", html).group(1)
                self.assertIn(b'data-event-mode="rehearsal"', attrs)
                self.assertIn(f'data-event-context="{self.context}"'.encode(), attrs)
                self.assertEqual(headers[CONTEXT.lower()], [self.context])
        for path, status in [("/api/desk/state", 401), ("/static/no-file", 404)]:
            _, headers = self.get(path, status)
            self.assertEqual(headers[CONTEXT.lower()], [self.context])

    def test_stale_and_missing_post_context_cannot_mutate_active_workspace(self):
        stale = self.context
        self.switch("rehearsal")
        judge = self.judge()
        issue = {"action": "create", "role": "participant", "label": "Stale person", "pair": 1, "member": 1}
        roster = {"rows": [{"pair": 1, "partner": "Stale company", "p1": "Stale name"}]}
        sheet = {"kind": "round", "pair": 9, "ticks": [1, 1, 1], "wrap_rating": 3}
        before, _ = self.get(WORKSPACE, cookie=self.admin)
        for marker in (stale, None):
            for path, data, authority in [
                ("/api/admin/credentials", issue, {"cookie": self.admin}),
                ("/api/desk/roster", roster, {"cookie": self.admin}),
                ("/api/submit", sheet, {"headers": judge}),
            ]:
                with self.subTest(path=path, context_present=marker is not None):
                    _, headers = self.post(path, data, 409, context=marker, **authority)
                    self.assertEqual(headers[CONTEXT.lower()], [self.context])
        after, _ = self.get(WORKSPACE, cookie=self.admin)
        self.assertEqual(before["summary"], after["summary"])
        roster_after, _ = self.get("/api/desk/roster", cookie=self.admin)
        self.assertEqual(roster_after["roster"][0]["p1"], "PracticePerson01A")
        self.post("/api/submit", sheet, headers=judge)
        fresh, _ = self.get(WORKSPACE, cookie=self.admin)
        self.assertEqual(fresh["summary"]["round_sheets"], before["summary"]["round_sheets"] + 1)

    def test_judge_sessions_and_both_presenter_key_scopes_cannot_cross(self):
        live_judge = self.judge()
        live_desk, _ = self.get("/api/desk/state", cookie=self.admin)
        live_keys = [live_desk["view_key"], live_desk["anonymous_view_key"]]
        self.switch("rehearsal")
        self.get("/api/state", 401, headers=live_judge)
        self.post("/api/submit", {"kind": "round", "pair": 9, "ticks": [1]}, 401, headers=live_judge)
        practice_judge = self.judge()
        practice_password = self.staff_password(2)
        practice_desk, _ = self.get("/api/desk/state", cookie=self.admin)
        practice_keys = [practice_desk["view_key"], practice_desk["anonymous_view_key"]]
        self.assertTrue(set(live_keys).isdisjoint(practice_keys))
        for key in practice_keys:
            state, _ = self.get("/api/public", headers={"X-Key": key})
            self.assertTrue(state["rehearsal"])
        for key in live_keys:
            self.get("/api/public", 401, headers={"X-Key": key})

        self.switch("live")
        self.get("/api/state", 401, headers=practice_judge)
        self.get("/api/state", 401, headers=live_judge)
        self.post("/api/access/login", {"role": "judge", "password": practice_password}, 403)
        for key in practice_keys:
            for path in ("/api/public", "/api/board"):
                with self.subTest(path=path, signed_in=False):
                    self.get(path, 401, headers={"X-Key": key})
                with self.subTest(path=path, signed_in=True):
                    self.get(path, 401, headers={"X-Key": key}, cookie=self.admin)
        for key in live_keys:
            state, _ = self.get("/api/public", headers={"X-Key": key})
            self.assertFalse(state["rehearsal"])

    def test_judge_draft_keys_separate_workspaces_and_changed_workspace_epochs(self):
        def sign_in():
            identity, _ = self.get('/api/access/me', headers=self.judge())
            return identity['draft_key']

        original_live = sign_in()
        self.assertEqual(sign_in(), original_live)
        self.switch("rehearsal")
        practice = sign_in()
        self.assertNotEqual(practice, original_live)
        self.assertEqual(sign_in(), practice)
        self.switch("live")
        renewed_live = sign_in()
        self.assertNotEqual(renewed_live, original_live)
        self.assertNotEqual(renewed_live, practice)

    def test_live_final_and_rehearsal_final_keep_separate_case_and_timer(self):
        def final(action, **fields):
            body = {"action": action, **fields}
            if action == "start":
                pending, _ = self.post("/api/desk/final", body, cookie=self.admin, status=409)
                body.update(override_reason="Test a Final in each isolated dataset.", review_token=pending["review_token"])
            self.post("/api/desk/final", body, cookie=self.admin)

        def timer(action, **fields):
            self.post("/api/desk/clock", {"action": action, **fields}, cookie=self.admin)

        final("set", pairs=[1,2,3])
        final("start")
        timer("pause")
        timer("seek", elapsed=90)
        self.switch("rehearsal")
        desk, _ = self.get("/api/desk/state", cookie=self.admin)
        self.assertEqual(desk["clock"]["kind"], "round")
        self.assertEqual(desk["clock"]["round"], 2)
        final("set", pairs=[9,1,2])
        final("start")
        timer("pause")
        timer("seek", elapsed=12)
        practice_judge, _ = self.get("/api/state", headers=self.judge())
        self.assertEqual(practice_judge["scenario"], "S1")  # Team 9 retains its original case.
        self.assertEqual(practice_judge["assignment"]["pair"], 9)
        self.assertEqual(practice_judge["clock"]["elapsed"], 12)
        self.switch("live")
        desk, _ = self.get("/api/desk/state", cookie=self.admin)
        self.assertEqual(desk["final"]["pairs"], [1,2,3])
        self.assertEqual(desk["clock"]["elapsed"], 90)


if __name__ == "__main__":
    unittest.main()
