#!/usr/bin/env python3
"""End-to-end test of the judging desk without opening a network port.

    python3 test_api.py [--ai]

Drives the real request handler in-process through a fake socket, so it also runs where sockets are blocked.
It simulates a desk, 8 judges, a full round, an edit, a void, a tie, the final, calibration and the AI endpoints.
"""
import io, json, os, sys, tempfile, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import server as S


class Sock:
    def __init__(self, data): self.rf = io.BytesIO(data); self.wf = io.BytesIO()
    def makefile(self, mode, *a, **k): return self.rf if "r" in mode else self.wf
    def sendall(self, b): self.wf.write(b)
    def setsockopt(self, *a): pass


class Srv: pass


def call(method, path, body=None, token=None, site=True, extra=None):
    raw = json.dumps(body).encode() if body is not None else b""
    head = "%s %s HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\nContent-Type: application/json\r\n" % (method, path, len(raw))
    if token: head += "X-Token: %s\r\n" % token
    if site: head += "X-Site: %s\r\n" % S.site_token()
    for k, v in (extra or {}).items(): head += "%s: %s\r\n" % (k, v)
    sock = Sock((head + "\r\n").encode() + raw)
    class H(S.Handler):
        def setup(self): self.rfile = sock.rf; self.wfile = sock.wf
        def finish(self): pass
    H(sock, ("127.0.0.1", 1), Srv())
    out = sock.wf.getvalue(); hdr, _, payload = out.partition(b"\r\n\r\n")
    code = int(hdr.split(b" ", 2)[1]); ct = b"json" in hdr.lower()
    try: return code, json.loads(payload) if ct else payload.decode()
    except ValueError: return code, payload.decode()


fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond: fails.append(name)


def main():
    tmp = tempfile.mkdtemp()
    S.CFG = S.load_config(os.path.join(tmp, "config.json")); S.CFG["rehearsal"] = True; S.CFG["mode"] = "desk"
    S.CONTENT = json.load(open(os.path.join(HERE, "content.json"), encoding="utf-8"))
    S.DB = S.open_db(os.path.join(tmp, "t.db")); S.DATA = tmp

    # site password and shared public links
    c, pg = call("GET", "/", site=False); check("landing page is gated", c == 200 and 'data-gate="1"' in pg)
    for path in ("/admin", "/desk", "/judge", "/judge/3", "/live", "/board"):
        c, pg = call("GET", path, site=False); check("%s is gated without password or key" % path, 'data-gate="1"' in pg, path)
    c, pg = call("GET", "/"); check("landing page opens with the password", 'data-gate="1"' not in pg and 'id="signin"' in pg and 'src="/static/common.js"' in pg)
    for path in ("/admin", "/desk", "/judge", "/judge/3"):
        c, pg = call("GET", path); check("%s opens with the password" % path, 'data-gate="1"' not in pg)
    c, pg = call("GET", "/judge"); check("/judge is the station list", "Judge stations" in pg)
    c, pg = call("GET", "/admin"); check("/admin is the Admin Panel", "Event Admin" in pg)
    c, r = call("GET", "/api/stations", site=False); check("station list needs the password", c == 401)
    c, r = call("GET", "/api/stations"); check("station list has 8 stations", c == 200 and len(r["stations"]) == 8 and all(x["judge"] == "" for x in r["stations"]), r)
    k = S.CFG["view_key"]
    c, pg = call("GET", "/live?key=" + k, site=False); check("/live opens with the key in the URL", 'data-gate="1"' not in pg)
    c, pg = call("GET", "/board?key=" + k, site=False); check("/board opens with the key in the URL", 'data-gate="1"' not in pg)
    c, pg = call("GET", "/live?key=wrong", site=False); check("wrong key stays gated", 'data-gate="1"' in pg)
    c, pg = call("GET", "/desk?key=" + k, site=False); check("the key does not open the desk page", 'data-gate="1"' in pg)
    c, r = call("GET", "/api/public", site=False); check("public data refused without key", c == 401)
    c, r = call("GET", "/api/public?key=" + k, site=False); check("public data with key", c == 200 and "rows" in r)
    c, r = call("GET", "/api/board", site=False, extra={"X-Key": k}); check("board data with key header", c == 200)
    c, r = call("GET", "/api/info", site=False); check("info refused without password", c == 401)
    c, r = call("GET", "/api/info"); check("info with password carries the key", c == 200 and r["view_key"] == k and r["authed"])
    c, r = call("GET", "/api/info?key=" + k, site=False); check("info with key does not reveal the key", c == 200 and "view_key" not in r)
    c, r = call("GET", "/static/judge.html", site=False); check("page files are not served as static files", c == 404)
    c, r = call("GET", "/static/app.css", site=False); check("shared css stays reachable for the sign-in page", c == 200)
    c, r = call("POST", "/api/site-login", {"password": "wrong"}, site=False); check("wrong site password refused", c == 403)
    import io as _io
    def raw_login(pw):
        sock = Sock(("POST /api/site-login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(json.dumps({"password": pw}))).encode() + json.dumps({"password": pw}).encode())
        class H(S.Handler):
            def setup(self): self.rfile = sock.rf; self.wfile = sock.wf
            def finish(self): pass
        H(sock, ("127.0.0.1", 1), Srv()); return sock.wf.getvalue().decode()
    out = raw_login(S.CFG["password"].upper()); check("right site password sets the cookie (case-insensitive)", "Set-Cookie: pp_site=%s" % S.site_token() in out and "HttpOnly" in out, out[:200])
    for _ in range(9): call("POST", "/api/site-login", {"password": "bad"}, site=False)
    c, r = call("POST", "/api/site-login", {"password": S.CFG["password"]}, site=False); check("repeated wrong passwords are throttled", c == 429, r)
    S.FAILS.clear()

    # sign in
    c, r = call("POST", "/api/login", {"role": "desk", "pin": "0000x"}); check("wrong desk PIN refused", c == 403)
    c, r = call("POST", "/api/login", {"role": "desk", "pin": S.CFG["desk_pin"]}); desk = r["token"]; check("desk sign-in", c == 200)
    c, r = call("POST", "/api/login", {"role": "judge", "station": 1, "name": ""}); check("judge login needs a name", c == 400)
    c, r = call("POST", "/api/login", {"role": "judge", "station": 1, "name": "x"}, site=False); check("judge login needs the event password first", c == 401)
    c, r = call("POST", "/api/login", {"role": "desk", "pin": S.CFG["password"]}); check("event password does not open the desk", c == 403)
    judges = {}
    for st in range(1, 9):
        c, r = call("POST", "/api/login", {"role": "judge", "station": st, "name": "J%d" % st}); judges[st] = r["token"]
    check("8 judges signed in", len(judges) == 8)
    c, r = call("GET", "/api/state", site=False, token=judges[1]); check("a valid judge token still works without the cookie", c == 200)
    c, _ = call("GET", "/api/desk/state", None, judges[1]); check("judge cannot open the desk", c == 403)
    c, _ = call("GET", "/api/desk/state"); check("no token refused", c == 401)

    # roster
    csv = "\n".join("%d, Partner %s, Person %da, Person %db, %d, %d" % (p, chr(64 + (p + 1) // 2), p, p, 1 if p % 3 == 0 else 0, 1 if p % 6 == 0 else 0) for p in range(1, 31))
    c, r = call("POST", "/api/desk/roster", {"csv": "pair,partner,p1,p2,c1,c2\n" + csv}, desk); check("roster saved", c == 200 and r["rows"] == 30, r)

    c, r = call("POST", "/api/desk/roster", {"rows": [{"pair": 1, "partner": " Acme ", "p1": "Ann", "p2": "Bo", "coach1": 1}, {"pair": 99, "partner": "x"}, {"pair": "z"}]}, desk)
    check("invalid roster rows are rejected atomically", c == 400, r)
    c, r = call("POST", "/api/desk/roster", {"rows": [{"pair": 1, "partner": " Acme ", "p1": "Ann", "p2": "Bo", "coach1": 1}]}, desk)
    check("valid roster table row saves", c == 200 and r["rows"] == 1, r)
    c, rr = call("GET", "/api/desk/roster", None, desk); check("table row stored trimmed", rr["roster"][0]["partner"] == "Acme" and rr["roster"][0]["coach1"] == 1, rr)
    call("POST", "/api/desk/roster", {"csv": "pair,partner,p1,p2,c1,c2\n" + csv}, desk)

    # content and state
    c, ct = call("GET", "/api/content", None, judges[3]); check("content served", c == 200 and len(ct["rubric"]) == 4 and len(ct["scenarios"]) == 4)
    c, st = call("GET", "/api/state", None, judges[3]); check("station 3 has pair 3 in round 1", st["assignment"]["pair"] == 3 and st["scenario"] == "S2", st["assignment"])
    check("clock idle", not st["clock"]["started_any"])

    # clock
    call("POST", "/api/desk/clock", {"action": "start"}, desk); time.sleep(1.1)
    c, st = call("GET", "/api/state", None, judges[1]); check("clock runs", st["clock"]["running"] and st["clock"]["elapsed"] >= 1, st["clock"])
    call("POST", "/api/desk/clock", {"action": "seek", "elapsed": 650}, desk)
    c, st = call("GET", "/api/state", None, judges[1]); check("phase = feedback at 10:50", st["clock"]["phase"] == "feedback", st["clock"])
    call("POST", "/api/desk/clock", {"action": "seek", "elapsed": 130}, desk)
    c, st = call("GET", "/api/state", None, judges[1]); check("step = Solution at Work at 2:10", st["clock"]["step"] == 1, st["clock"])

    # round 1: judges submit; station s scores 2*s+? ticks
    def sheet(station, pair, nt, ai=0, kind="round", **kw):
        b = {"kind": kind, "pair": pair, "ticks": [1] * nt + [0] * (10 - nt), "ai": ai, "spoke": 1, "notes": "[Discover 0:30] ok", "feedback": "Good.", "judge": "J%d" % station,
             "asked": [{"q": "Q1", "t": 20}, {"q": "Q2", "t": 40}], "raised": []}
        if kind == "round": b["wrap_rating"] = 3
        if kind == "final": b.update(run_id=S.kv_get("final")["run_id"],final_rating=max(1,min(5,nt)))
        b.update(kw); return call("POST", "/api/submit", b, judges[station])
    c, r = sheet(1, 2, 5); check("wrong pair rejected", c == 409, r)
    plan = {1: (1, 8, 1), 2: (2, 8, 0), 3: (3, 6, 0), 4: (4, 6, 1), 5: (5, 9, 0), 6: (6, 4, 0), 7: (7, 10, 1), 8: (8, 3, 0)}
    for st_, (pair, nt, ai) in plan.items():
        c, r = sheet(st_, pair, nt, ai); assert c == 200, r
    c, lb = call("GET", "/api/desk/scores", None, desk); L = lb["leaderboard"]
    check("8 sheets in leaderboard", len(L) == 8)
    top = L[0]
    badge = lambda p: min(4, 2 * ((1 if p % 3 == 0 else 0) + (1 if p % 6 == 0 else 0)))
    check("score = ticks x10 + AI x2 + badge", top["pair"] == 7 and top["total"] == 100 + 2 + badge(7), top)
    check("pair 6 gets badge +4", [x for x in L if x["pair"] == 6][0]["badge"] == 4)
    check("order descending", [x["total"] for x in L] == sorted([x["total"] for x in L], reverse=True))

    # resubmit replaces
    c, r = sheet(6, 6, 7); L = call("GET", "/api/desk/scores", None, desk)[1]["leaderboard"]
    check("resubmit replaces (still 8 sheets)", len(L) == 8 and [x for x in L if x["pair"] == 6][0]["ticks"] == 7)
    # desk edit and void
    sid = [x for x in L if x["pair"] == 8][0]["sheet"]
    call("POST", "/api/desk/sheet", {"id": sid, "action": "ticks", "ticks": [1] * 5 + [0] * 5, "ai": 0}, desk)
    L = call("GET", "/api/desk/scores", None, desk)[1]["leaderboard"]; check("desk edit applied", [x for x in L if x["pair"] == 8][0]["ticks"] == 5)
    call("POST", "/api/desk/sheet", {"id": sid, "action": "void"}, desk)
    L = call("GET", "/api/desk/scores", None, desk)[1]["leaderboard"]; check("void removes the sheet", len(L) == 7)
    c, ds = call("GET", "/api/desk/state", None, desk); check("desk shows station 8 not submitted", not ds["stations"][7]["submitted"] and ds["stations"][0]["submitted"])

    # tie-break
    sheet(8, 8, 8, 0)    # pair 8: 80 pts, badge 0
    # make an exact tie with pair 1 (8 ticks + ai = 82, badge 0): use pair 8 with ai
    sheet(8, 8, 8, 1)
    L = call("GET", "/api/desk/scores", None, desk)[1]["leaderboard"]
    p1 = [x for x in L if x["pair"] == 1][0]; p8 = [x for x in L if x["pair"] == 8][0]
    check("tie detected on equal total, solve and discover", p1["total"] == p8["total"] and (p1["tie"] or p1["groups"] != p8["groups"]), (p1, p8))

    # flags
    f = call("GET", "/api/desk/scores", None, desk)[1]["flags"]; check("flag for all ten ticks", any("all ten" in x["text"] for x in f), f)

    # next round
    call("POST", "/api/desk/clock", {"action": "next"}, desk)
    c, st = call("GET", "/api/state", None, judges[2]); check("round 2: station 2 has pair 10", st["assignment"]["pair"] == 10, st["assignment"])
    c, r = sheet(2, 2, 5); check("old pair rejected in round 2", c == 409)
    c, r = sheet(2, 10, 5); check("round 2 sheet accepted", c == 200)
    call("POST", "/api/desk/clock", {"action": "goto", "round": 4}, desk)
    c, st = call("GET", "/api/state", None, judges[7]); check("round 4 station 7: no pair (30 pairs)", st["assignment"]["pair"] is None, st["assignment"])
    c, st = call("GET", "/api/state", None, judges[6]); check("round 4 station 6 = pair 30", st["assignment"]["pair"] == 30)
    call("POST", "/api/desk/clock", {"action": "goto", "round": 1}, desk)

    # calibration
    call("POST", "/api/desk/calib", {"active": True}, desk)
    c, st = call("GET", "/api/state", None, judges[5]); check("calibration assignment", st["assignment"]["kind"] == "calib" and st["scenario"] == "MOCK")
    key = [1 if S.CONTENT["mock"]["key"][i]["earned"] else 0 for i in ["D1", "D2", "D3", "M1", "M2", "S1", "S2", "S3", "C1", "C2"]]
    c, r = call("POST", "/api/submit", {"kind": "calib", "pair": 0, "ticks": key, "judge": "J5", "feedback": ""}, judges[5]); check("calibration submit", c == 200, r)
    wrong = list(key); wrong[3] ^= 1; wrong[7] ^= 1
    call("POST", "/api/submit", {"kind": "calib", "pair": 0, "ticks": wrong, "judge": "J6", "feedback": ""}, judges[6])
    c, cal = call("GET", "/api/desk/calib", None, desk); offs = sorted(r["off"] for r in cal["rows"]); check("calibration compares to the key", offs == [0, 2], offs)
    call("POST", "/api/desk/calib", {"active": False}, desk)

    # final
    L = call("GET", "/api/desk/scores", None, desk)[1]["leaderboard"]
    top3 = [x["pair"] for x in L[:3]]
    call("POST", "/api/desk/final", {"action": "set", "pairs": top3}, desk); call("POST", "/api/desk/final", {"action": "start"}, desk)
    c, st = call("GET", "/api/state", None, judges[4]); check("final assigns the first finalist to every judge", st["assignment"]["kind"] == "final" and st["assignment"]["pair"] == top3[0])
    c, r = sheet(4, top3[1], 5, kind="final"); check("final: wrong pair rejected", c == 409)
    for stn in range(1, 9):
        sheet(stn, top3[0], 6 + (stn % 3), 1 if stn % 2 else 0, kind="final")
    fb = call("GET", "/api/desk/scores", None, desk)[1]["final"]
    f0 = [x for x in fb if x["pair"] == top3[0]][0]
    exp_t = sum(6 + (s % 3) for s in range(1, 9)) / 8; exp_ai = sum(1 for s in range(1, 9) if s % 2) / 8
    check("final averages 8 judges", f0["judges"] == 8 and abs(f0["total"] - round(exp_t * 10 + exp_ai * 2 + badge(top3[0]), 2)) < 0.01, f0)
    c, _ = call("POST", "/api/desk/final", {"action": "stop"}, desk)
    check("Final review waits for every finalist's panel sheets", c == 409)
    for i, finalist in enumerate(top3[1:], 1):
        c, _ = call("POST", "/api/desk/final", {"action": "next"}, desk)
        check("Final advances after complete receipts", c == 200)
        for stn in range(1, 9):
            c, _ = sheet(stn, finalist, 5-i, kind="final")
            assert c == 200
    c, _ = call("POST", "/api/desk/final", {"action": "stop"}, desk)
    check("Final enters review after all panel sheets", c == 200)

    # board privacy
    c, b = call("GET", "/api/board"); check("board hides scores by default", "podium" not in b)
    call("POST", "/api/desk/reveal", {"mode": "top3"}, desk); c, b = call("GET", "/api/board"); check("board shows podium after reveal", len(b.get("podium", [])) == 3)
    call("POST", "/api/desk/reveal", {"mode": "hidden"}, desk)
    c, txt = call("GET", "/api/board"); check("board payload has no sheet detail", "ticks" not in json.dumps(txt))

    # exports
    c, csvt = call("GET", "/api/desk/export.csv", token=desk); check("csv export", c == 200 and "rank,pair" in csvt)
    c, cards = call("GET", "/api/desk/cards", token=desk); check("feedback cards render", c == 200 and "<section>" in cards)
    c, bk = call("GET", "/api/desk/backup.json", token=desk); check("backup has sheets", c == 200 and len(bk["sheets"]) > 10)

    # template feedback and AI
    tpl = S.template_feedback([1, 1, 0, 1, 0, 0, 0, 0, 1, 0]); check("template feedback is plain text", "worked" in tpl and "add" in tpl.lower(), tpl)
    c, r = call("POST", "/api/ai/feedback", {"ticks": [1, 1, 0, 1, 0, 0, 0, 0, 1, 0], "notes": "[Solve 6:00] never named a service", "asked": [{"q": "Q1"}], "scenario": "S1"}, judges[1])
    check("ai feedback endpoint always answers", c == 200 and r["text"], r)
    print("  feedback source:", r.get("source"), "|", r["text"][:200].replace("\n", " "), "|", r.get("why", ""))
    check("standalone feedback uses the template", r["source"] == "template")
    c, r = call("POST", "/api/ai/tidy", {"notes": "Original notes"}, judges[1])
    check("standalone tidy leaves notes intact", c == 200 and r["source"] == "unchanged" and r["text"] == "Original notes")

    # judge-paced sessions (mode = self)
    call("POST", "/api/desk/reset", {"confirm": "RESET"}, desk)
    call("POST", "/api/desk/roster", {"csv": "pair,partner,p1,p2,c1,c2\n" + csv}, desk)
    c, r = call("POST", "/api/session", {"action": "start"}, judges[1]); check("self: judge cannot start in desk mode", c == 409, r)
    c, r = call("POST", "/api/desk/mode", {"mode": "self"}, desk); check("self: desk switches mode", c == 200 and r["mode"] == "self", r)
    c, st = call("GET", "/api/state", None, judges[1]); check("self: state says self, idle", st["mode"] == "self" and not st["clock"]["started_any"] and st["has_next"], st)
    c, r = call("POST", "/api/session", {"action": "start"}, judges[1]); check("self: judge 1 starts", c == 200 and r["clock"]["running"], r)
    c, st2 = call("GET", "/api/state", None, judges[2]); check("self: station 2 unaffected", not st2["clock"]["running"])
    time.sleep(1.1)
    c, st = call("GET", "/api/state", None, judges[1]); check("self: judge 1 clock runs", st["clock"]["elapsed"] >= 1, st["clock"])
    c, r = call("POST", "/api/session", {"action": "feedback"}, judges[1]); check("self: end pitch jumps to feedback", r["clock"]["phase"] == "feedback", r)
    c, r = call("POST", "/api/session", {"action": "pause"}, judges[1]); check("self: pause", not r["clock"]["running"])
    c, r = call("POST", "/api/session", {"action": "next"}, judges[1]); check("self: next blocked before submit", c == 409 and r.get("needs_force"), r)
    c, r = sheet(1, 1, 7); check("self: sheet for pair 1 accepted", c == 200, r)
    c, r = call("POST", "/api/session", {"action": "finish"}, judges[1]); check("self: finish ends session", r["clock"]["ended"] and not r["clock"]["running"], r)
    c, r = call("POST", "/api/session", {"action": "next"}, judges[1]); check("self: next after submit", c == 200 and r["clock"]["round"] == 2 and not r["clock"]["started_any"], r)
    c, st = call("GET", "/api/state", None, judges[1]); check("self: judge 1 now has pair 9", st["assignment"]["pair"] == 9, st["assignment"])
    c, st = call("GET", "/api/state", None, judges[2]); check("self: judge 2 still pair 2", st["assignment"]["pair"] == 2, st["assignment"])
    c, r = sheet(2, 2, 5); check("self: station 2 submits at its own pace", c == 200, r)
    c, r = sheet(1, 1, 5); check("self: station 1 old pair rejected", c == 409)
    c, d = call("GET", "/api/desk/state", None, desk); s1 = d["stations"][0]; s2 = d["stations"][1]
    check("self: desk sees per-station rounds", s1["clock"]["round"] == 2 and s2["clock"]["round"] == 1 and s2["submitted"] and not s1["submitted"], (s1["pair"], s2["pair"]))
    c, r = call("POST", "/api/desk/clock", {"action": "start", "station": 3}, desk); check("self: desk starts one station", call("GET", "/api/state", None, judges[3])[1]["clock"]["running"] and not call("GET", "/api/state", None, judges[4])[1]["clock"]["running"])
    call("POST", "/api/desk/clock", {"action": "start"}, desk); check("self: desk starts all", all(call("GET", "/api/state", None, judges[i])[1]["clock"]["running"] for i in range(1, 9)))
    c, b = call("GET", "/api/board"); check("self: board lists station clocks without scores", len(b["station_clocks"]) == 8 and "ticks" not in json.dumps(b))
    for _ in range(3): call("POST", "/api/session", {"action": "finish"}, judges[8]); call("POST", "/api/session", {"action": "next", "force": True}, judges[8])
    c, r = call("POST", "/api/session", {"action": "next", "force": True}, judges[8]); check("self: no pair after the last session", c == 409, r)
    c, st = call("GET", "/api/state", None, judges[8]); check("self: station 8 stops at pair 24 (session 4 would be pair 32, which does not exist)", st["assignment"]["pair"] == 24 and not st["has_next"], st["assignment"])
    c, r = call("POST", "/api/desk/mode", {"mode": "desk"}, desk); check("self: desk switches back", r["mode"] == "desk")
    c, r = call("POST", "/api/session", {"action": "start"}, judges[1]); check("self: judge blocked again in desk mode", c == 409)

    from unittest.mock import patch
    from test_event_access import request as hosted_request
    with patch.dict(os.environ, {"KEVENOS_HOSTED": "1", "KEVENOS_GATEWAY_ENABLED": "1"}):
        c, admin, admin_headers = hosted_request("POST", "/api/access/login", {"role": "admin", "password": S.CFG["password"]})
        assert c == 200, "Hosted Admin fixture sign-in failed"
        host_desk = next(value.split(';', 1)[0] for value in admin_headers['set-cookie'] if value.startswith('pp_access='))
        c, issued, _ = hosted_request("POST", "/api/admin/credentials", {"action": "create", "role": "judge", "station": 1, "label": "Example Judge"}, host_desk)
        assert c == 200, "Hosted Staff fixture creation failed"
        c, judge, judge_headers = hosted_request("POST", "/api/access/login", {"role": "judge", "password": issued['password']})
        assert c == 200 and judge['station'] == 1, "Hosted Staff fixture sign-in failed"
        host_judge = next(value.split(';', 1)[0] for value in judge_headers['set-cookie'] if value.startswith('pp_access='))
        c, _ = call("GET", "/api/desk/export.csv?t=" + host_desk.split('=', 1)[1])
        check("admin export refuses a query token", c == 401)
        c, _ = call("GET", "/api/desk/export.csv", token=host_desk.split('=', 1)[1], site=False)
        check("hosted access requires the cookie rather than an old bearer token", c == 401)
        c, _, _ = hosted_request("GET", "/api/desk/export.csv", cookie=host_judge)
        check("judge cannot export admin scores", c == 403)
        check("hosted event cookie is secure", any("HttpOnly; SameSite=Lax; Secure" in value for value in admin_headers['set-cookie']))
        S.GATEWAY_HEALTH["t"] = float("-inf")
        with patch.object(S, "gateway_request") as gateway:
            gateway.side_effect = [
                {"data": [{"id": "tier/mid"}]},
                {"choices": [{"message": {"content": "You asked good questions. Well done."}}]},
                {"choices": [{"message": {"content": "Good discovery questions."}}]},
                RuntimeError("provider-private-error"),
            ]
            check("health probes the gateway role", S.gateway_ready() and gateway.call_args.args == ("GET", "/v1/models"))
            c, r, _ = hosted_request("POST", "/api/ai/feedback", {"ticks": [1, 1], "scenario": "S1"}, host_judge)
            request = gateway.call_args.args
            check("hosted feedback uses the OpenAI-compatible AI service", c == 200 and r["source"] == "ai" and request[:2] == ("POST", "/v1/chat/completions") and request[2]["model"] == "tier/mid" and request[2]["reasoning_effort"] == "none")
            c, r, _ = hosted_request("POST", "/api/desk/ai-summary", {}, host_desk)
            prompt = gateway.call_args.args[2]["messages"][1]["content"]
            check("debrief sends aggregate rubric counts", c == 200 and r["source"] == "ai" and "Tick rates" in prompt and "Judge notes" not in prompt and "Top pairs" not in prompt)
            c, r, _ = hosted_request("POST", "/api/ai/tidy", {"notes": "Original notes"}, host_judge)
            check("gateway errors stay private and preserve notes", c == 200 and r["text"] == "Original notes" and "provider-private-error" not in str(r))
        with tempfile.TemporaryDirectory() as secrets_dir, patch.dict(os.environ, {"KEVENOS_SECRETS_DIR": secrets_dir, "KEVENOS_AI_BASE_URL": S.GATEWAY_URL}):
            for key in ("KEVENOS_PASSWORD", "KEVENOS_VIEW_KEY", "KEVENOS_SESSION_SECRET", "KEVENOS_GUEST_PASSWORD", "KEVENOS_AI_API_KEY"):
                with open(os.path.join(secrets_dir, key), "w") as f:
                    f.write("test-" + key)
            persisted = os.path.join(secrets_dir, "config.json")
            hosted = S.load_config(persisted)
            check("hosted credentials come from mounts without a PIN and are not persisted", "desk_pin" not in hosted and hosted["password"] == "test-KEVENOS_PASSWORD" and hosted["ai_api_key"] == "test-KEVENOS_AI_API_KEY" and not os.path.exists(persisted))
            with open(persisted, "w") as f:
                json.dump({"password": "test-plaintext"}, f)
            try:
                S.load_config(persisted)
            except RuntimeError:
                refused = True
            else:
                refused = False
            check("hosted app refuses legacy plaintext credentials", refused)
    check("CSV escapes formula-like text and preserves numeric scores", S.csv_cell(" =value") == "' =value" and S.csv_cell(40) == 40)

    # versions and editable content
    shipped_version = S.CONTENT["content_version"]
    c, inf = call("GET", "/api/info"); check("info carries app and content versions", inf["version"]["app"] == S.APP_VERSION and inf["version"]["content"] == shipped_version, inf.get("version"))
    c, cc = call("GET", "/api/desk/content", None, desk); check("desk can read the content", c == 200 and "rubric" in cc["content"] and cc["versions"]["content"] == shipped_version)
    c, r = call("GET", "/api/desk/content", None, judges[1]); check("judges cannot read the editor endpoint", c == 403)
    import copy
    doc = copy.deepcopy(cc["content"]); doc["rubric"][0]["ticks"][0]["text"] = "EDITED criterion text"; doc["questions"][0]["items"][1]["text"] = "EDITED question"
    c, r = call("POST", "/api/desk/content", {"content": doc, "note": "reworded D1"}, judges[1]); check("judges cannot save content", c == 403)
    c, r = call("POST", "/api/desk/content", {"content": doc, "note": "reworded D1"}, desk); check("content saved as v2", c == 200 and r["version"] == shipped_version + 1, r)
    check("new text is live", S.CONTENT["rubric"][0]["ticks"][0]["text"] == "EDITED criterion text" and os.path.exists(os.path.join(S.DATA, "content.json")))
    c, st = call("GET", "/api/state", None, judges[1]); check("judges see content v2 in their state", st["versions"]["content"] == shipped_version + 1 and st["versions"]["content_note"] == "reworded D1", st["versions"])
    c, cj = call("GET", "/api/content", None, judges[1]); check("judges load the edited text", cj["rubric"][0]["ticks"][0]["text"] == "EDITED criterion text" and cj["content_version"] == shipped_version + 1)
    bad = copy.deepcopy(doc); bad["rubric"][1]["ticks"].pop()
    c, r = call("POST", "/api/desk/content", {"content": bad}, desk); check("dropping a criterion is refused", c == 400 and "ticks" in r["error"], r)
    bad = copy.deepcopy(doc); bad["questions"][0]["items"] = [q for q in bad["questions"][0]["items"] if q["id"] != "Q1"]
    c, r = call("POST", "/api/desk/content", {"content": bad}, desk); check("removing a protected question is refused", c == 400, r)
    bad = copy.deepcopy(doc); del bad["scenarios"]
    c, r = call("POST", "/api/desk/content", {"content": bad}, desk); check("missing section is refused", c == 400, r)
    check("refused saves leave the version alone", S.CONTENT["content_version"] == shipped_version + 1)
    ap = call("GET", "/api/state", None, judges[1])[1]["assignment"]["pair"]
    c, r = sheet(1, ap, 6); sh = [x for x in S.active_sheets("round") if x["pair"] == ap][-1] if c == 200 else {}
    check("sheet records app and content version", sh.get("app_version") == S.APP_VERSION and sh.get("content_version") == str(shipped_version + 1), sh.get("content_version"))
    call("POST", "/api/desk/content", {"action": "bundled", "expected_content_version": S.CONTENT["content_version"]}, desk)
    check("revert to shipped text is a new version", S.CONTENT["content_version"] == shipped_version + 2 and S.CONTENT["rubric"][0]["ticks"][0]["text"] != "EDITED criterion text")
    c, sc = call("GET", "/api/desk/scores", None, desk); check("mixed content versions are flagged", any("different content versions" in f["text"] for f in sc["flags"]) or len({x.get("content_version") for x in S.active_sheets("round")}) < 2, sc["flags"][-2:])
    check("history lists versions", [h["version"] for h in call("GET", "/api/desk/content", None, desk)[1]["history"]] == [shipped_version + 2, shipped_version + 1])
    c, old = call("GET", "/api/desk/content?v=" + str(shipped_version + 1), None, desk); check("an earlier version can be loaded", old["content"]["rubric"][0]["ticks"][0]["text"] == "EDITED criterion text")
    db2 = S.open_db(os.path.join(tmp, "t.db")); check("schema version stored", S.kv_get("schema_version") == S.SCHEMA_VERSION)

    # public live view
    c, pb = call("GET", "/api/public"); check("public view needs no sign-in and lists ranked pairs", c == 200 and not pb["off"] and pb["scored"] > 0 and pb["rows"][0]["rank"] == 1, pb.get("scored"))
    txt = json.dumps({"rows": pb["rows"], "stations": pb["stations"]}); check("public scores and stations carry no notes, feedback or judge names", all(k not in txt for k in ("notes", "feedback", "judge", "asked")), txt[:200])
    check("public totals match the leaderboard", [r["total"] for r in pb["rows"]] == [r["total"] for r in S.leaderboard()])
    c, r = call("POST", "/api/desk/public", {"mode": "off"}, judges[1]); check("judges cannot switch the public view", c == 403)
    c, r = call("POST", "/api/desk/public", {"mode": "off"}, desk); c, pb = call("GET", "/api/public"); check("public view can be switched off", pb["off"] and pb["rows"] == [])
    call("POST", "/api/desk/public", {"mode": "live"}, desk)
    check("public view carries the program and stations", pb["program"]["total"] == 12 and len(pb["stations"]) == 8 and pb["program"]["items"][7]["title"] == "Pitch rounds", pb["program"]["total"])
    c, r = call("POST", "/api/desk/program", {"action": "next"}, judges[1]); check("judges cannot change the display step", c == 403)
    c, r = call("POST", "/api/desk/program", {"action": "set", "index": 7}, desk); check("desk sets the step", c == 200 and call("GET", "/api/public")[1]["program"]["index"] == 7)
    call("POST", "/api/desk/program", {"action": "next"}, desk); call("POST", "/api/desk/program", {"action": "next"}, desk); call("POST", "/api/desk/program", {"action": "next"}, desk)
    call("POST", "/api/desk/program", {"action": "next"}, desk); call("POST", "/api/desk/program", {"action": "next"}, desk); call("POST", "/api/desk/program", {"action": "next"}, desk)
    check("step stops at the last item", call("GET", "/api/public")[1]["program"]["index"] == 11)
    call("POST", "/api/desk/program", {"action": "announce", "text": "Hello room"}, desk); check("announcement shows", call("GET", "/api/public")[1]["program"]["announce"] == "Hello room")
    call("POST", "/api/desk/program", {"action": "mode", "mode": "clock"}, desk); check("clock mode picks an item", 0 <= call("GET", "/api/public")[1]["program"]["index"] <= 11)
    call("POST", "/api/desk/program", {"action": "set", "index": 0}, desk); call("POST", "/api/desk/program", {"action": "announce", "text": ""}, desk)
    pub = lambda: call("GET", "/api/public")[1]
    check("step 1 is an instructions scene", pub()["scene"] == "info")
    call("POST", "/api/desk/program", {"action": "set", "index": 6}, desk); check("preparing step uses the prep scene", pub()["scene"] == "prep")
    call("POST", "/api/desk/program", {"action": "set", "index": 7}, desk); check("pitch rounds use the pitch scene", pub()["scene"] == "pitch")
    c, r = call("POST", "/api/desk/program", {"action": "scene", "scene": "scores"}, judges[1]); check("judges cannot change the scene", c == 403)
    call("POST", "/api/desk/program", {"action": "scene", "scene": "scores"}, desk); check("admin overrides the scene", pub()["scene"] == "scores" and pub()["scene_override"] == "scores")
    c, r = call("POST", "/api/desk/program", {"action": "scene", "scene": "bogus"}, desk); check("unknown scene refused", c == 400 and pub()["scene"] == "scores")
    call("POST", "/api/desk/program", {"action": "set", "index": 9}, desk); check("choosing a step goes back to automatic", pub()["scene"] == "final" and pub()["scene_override"] == "auto")
    check("final info lists finalists", "final" in pub() and isinstance(pub()["final"]["pairs"], list))
    call("POST", "/api/desk/reveal", {"mode": "hidden"}, desk); check("no podium until revealed", "podium" not in pub())
    call("POST", "/api/desk/reveal", {"mode": "top3"}, desk); check("podium after the admin reveals it", len(pub().get("podium", [])) == 3)
    call("POST", "/api/desk/reveal", {"mode": "hidden"}, desk)
    call("POST", "/api/desk/program", {"action": "set", "index": 0}, desk)
    c, pg = call("GET", "/live"); check("live page is served", c == 200 and "KevenOS" in pg)

    call("POST", "/api/desk/final", {"action": "set", "pairs": [1, 2, 3]}, desk)
    check("staged finalists stay private", pub()["final"]["pairs"] == [])
    call("POST", "/api/desk/final", {"action": "start"}, desk)
    check("live final includes its announced roster", [p["pair"] for p in pub()["final"]["pairs"]] == [1, 2, 3])
    call("POST", "/api/desk/public", {"mode": "off"}, desk)
    check("hidden leaderboard keeps aggregate progress", pub()["rows"] == [] and pub()["scored"] > 0)
    old_content = S.bundled_content()
    old_content["agenda"][0]["line"] = "Edited schedule copy"
    for step in old_content["agenda"]:
        step.pop("scene", None)
    with open(S.content_path(), "w") as f:
        json.dump(old_content, f)
    migrated = S.load_content()
    check("older saved content keeps text and restores the default scenes", migrated["agenda"][0]["line"] == "Edited schedule copy" and migrated["agenda"][7]["scene"] == "pitch")

    # reset
    c, _ = call("POST", "/api/desk/reset", {"confirm": "no"}, desk); check("reset needs the word", c == 400)
    c, _ = call("POST", "/api/desk/reset", {"confirm": "RESET"}, desk); check("reset works", c == 200 and len(call("GET", "/api/desk/scores", None, desk)[1]["leaderboard"]) == 0)
    print("\n%d failed" % len(fails) if fails else "\nall passed"); sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
