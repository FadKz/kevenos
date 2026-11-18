#!/usr/bin/env python3
"""KevenOS Admin: a small web service with an Admin Panel, a Judge Page and a Live View for the event.

    python3 server.py                 # serve on the port in config.json (default 8080)
    python3 server.py --db rehearsal.db --port 8090

Judges open the configured event origin in a supported browser and sign in with
their assigned credential.
Scoring is plain arithmetic (ticks x 10 + AI tick x 2 + badge bonus). The OpenAI-compatible AI service only helps with words: feedback drafts, tidying notes and an end-of-event summary. If the model is down, everything
still works and the feedback falls back to a template.
"""
import argparse, csv, hashlib, hmac, html, io, json, math, os, re, secrets, socket, sqlite3, subprocess
import http.client
import event_roles
import database
import event_access
import event_mail
import event_workspace
import agent_api
import event_config
import final_event
import event_profiles
import admin_agenda
import event_assistant
import event_operations
import participant_workspace
import event_quiz
import audience_questions
import event_push
import registration_desk
from contextlib import nullcontext
import sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_VERSION = "1.1.0"        # bump with every change to the app; list it in CHANGELOG.md
SCHEMA_VERSION = 14          # bump when the database layout changes (open_db migrates)
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DATA = os.environ.get("KEVENOS_DATA") or os.path.join(HERE, "data")    # KEVENOS_DATA: a mounted volume when run in a container
ROUND_SECONDS = 900          # 10 pitch + 3 feedback + 2 change over
FINAL_SECONDS = 480
PITCH_END, FEEDBACK_END = 600, 780
GROUPS = [(0, 3), (3, 5), (5, 8), (8, 10)]   # Discover, Solution at Work, Solve, Close
STEP_NAMES = ["Discover", "Solution at Work", "Solve", "Close"]
STEP_ENDS = [120, 300, 480, 600]
LOCK = threading.RLock()
REQUEST_AUTH = threading.local()
CONTENT = {}
CFG = {}
DB = None
AUTH_DB = None
WORKSPACE = None
STARTING = False
READY = threading.Event()
LIVE = {}                    # station -> last heartbeat
AI_SLOTS = threading.Semaphore(2)
INVITE_TTL = 48 * 3600
JUDGE_TTL = 24 * 3600
ORGANISER_TTL = 4 * 3600
GATEWAY_URL = os.environ.get("KEVENOS_AI_BASE_URL", "")
GATEWAY_ROLE = "tier/mid"
GATEWAY_SOCKET = os.environ.get("KEVENOS_AI_SOCKET", "")
GATEWAY_HEALTH = {"t": float("-inf"), "ok": False}
GATEWAY_HEALTH_LOCK = threading.Lock()
AI_FALLBACK = "AI is unavailable. You can continue with the original notes or a template."


# ───────────────────────── setup ─────────────────────────
WORDS = ["amber", "birch", "cedar", "delta", "ember", "fjord", "grove", "harbor", "indigo", "jasper", "kite", "lotus", "maple", "nova", "olive", "pearl", "quartz", "river", "sage", "tulip"]


def hosted():
    return os.environ.get("KEVENOS_HOSTED") == "1"


def gateway_enabled():
    return hosted() and os.environ.get("KEVENOS_GATEWAY_ENABLED") == "1" and bool(GATEWAY_URL)


def load_config(path):
    cfg = {"port": 8080, "pairs": 30, "stations": 8,
           "rounds": 4, "rehearsal": False, "mode": "self"}
    if hosted():
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                persisted = json.load(f)
            if {"desk_pin", "password", "view_key", "secret", "station_pins", "ai_api_key", "guest_password"} & persisted.keys():
                raise RuntimeError("hosted config contains credentials; migrate them to secret mounts")
            cfg.update(persisted)
        secret_dir = os.environ.get("KEVENOS_SECRETS_DIR", "/run/secrets")
        for key, filename in (("password", "KEVENOS_PASSWORD"),
                              ("view_key", "KEVENOS_VIEW_KEY"), ("secret", "KEVENOS_SESSION_SECRET"),
                              ("guest_password", "KEVENOS_GUEST_PASSWORD")):
            with open(os.path.join(secret_dir, filename), encoding="utf-8") as f:
                cfg[key] = f.read().strip()
            if not cfg[key]:
                raise RuntimeError("empty hosted secret: " + key)
        if gateway_enabled():
            url = os.environ.get("KEVENOS_AI_BASE_URL", "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise RuntimeError("KEVENOS_AI_BASE_URL must be an HTTPS OpenAI-compatible endpoint")
            with open(os.path.join(secret_dir, "KEVENOS_AI_API_KEY"), encoding="utf-8") as f:
                cfg["ai_api_key"] = f.read().strip()
            if not cfg["ai_api_key"]:
                raise RuntimeError("empty hosted gateway key")
        return cfg
    if os.path.exists(path):
        with open(path, encoding="utf-8") as source:
            cfg.update(json.load(source))
    changed = False
    if "desk_pin" not in cfg:
        cfg["desk_pin"] = "%04d" % secrets.randbelow(10000); changed = True
    if "password" not in cfg:     # one shared password for the invited judges
        cfg["password"] = "-".join(secrets.choice(WORDS) for _ in range(2)) + "-%02d" % secrets.randbelow(100); changed = True
    if "view_key" not in cfg:     # goes in the web address of the shared public pages (/live, /board)
        cfg["view_key"] = secrets.token_hex(5); changed = True
    if cfg.pop("station_pins", None) is not None:
        changed = True
    if "secret" not in cfg:
        cfg["secret"] = secrets.token_hex(16); changed = True
    if changed or not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    return cfg


def open_db(path):
    db = database.connect(path)
    with database.migration_lock(db):
        # Refuse future schemas before changing journal mode or creating tables.
        if database.table_exists(db, "kv"):
            row=db.execute("SELECT v FROM kv WHERE k='schema_version'").fetchone()
            current_version = int(json.loads(row[0])) if row else 0
            if current_version>SCHEMA_VERSION:
                db.close()
                raise RuntimeError('Database schema is newer than this application. Use the matching release.')
            # Managed PostgreSQL cold starts must not replay every historical
            # migration. A schema version is written only after all migrations
            # complete, so an exact match is a safe constant-time fast path.
            if database.is_server(db) and current_version == SCHEMA_VERSION:
                return db
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
    CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS roster (pair INTEGER PRIMARY KEY, partner TEXT, p1 TEXT, p2 TEXT, coach1 INTEGER DEFAULT 0, coach2 INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sheets (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, pair INTEGER, station INTEGER, round INTEGER,
      judge TEXT, ticks TEXT, ai INTEGER, spoke INTEGER, notes TEXT, feedback TEXT, asked TEXT, raised TEXT,
      created REAL, voided INTEGER DEFAULT 0, edited_by TEXT);
    CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS judge_invites (
      station INTEGER PRIMARY KEY, digest TEXT NOT NULL, judge TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS role_sessions (
      digest TEXT PRIMARY KEY, role TEXT NOT NULL, station INTEGER NOT NULL, judge TEXT NOT NULL, expires REAL NOT NULL);
        """)
        agent_api.migrate(db)
        event_assistant.migrate(db)
        event_access.migrate(db)
        event_mail.migrate(db)
        event_config.migrate(db)
        final_event.migrate(db)
        participant_workspace.migrate(db)
        registration_desk.migrate(db)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(sheets)")}
        for col in ("app_version", "content_version"):       # schema 2: every sheet records the app and content it was scored on
            if col not in cols:
                db.execute("ALTER TABLE sheets ADD COLUMN %s TEXT" % col)
        db.execute("INSERT INTO kv(k,v) VALUES('schema_version',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(SCHEMA_VERSION),))
        db.commit()
    return db


def kv_get(k, default=None):
    with LOCK:
        r = DB.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(k, v, *, quiz_before=None):
    with LOCK:
        # Once a manual selection has a stable identity, keep every subsequent
        # Desk/API selection in sync with its numeric index for older clients.
        if k == 'program' and isinstance(v, dict) and 'item_id' in v:
            import event_commands
            items = event_commands._agenda_items(CONTENT)
            index = max(0, min(len(items)-1, int(v.get('i', 0)))) if items else 0
            v = dict(v, item_id=items[index]['id'] if items else None)
        if k == event_quiz.KEY and event_push.quiz_changed(sys.modules[__name__],
                quiz_before if quiz_before is not None else kv_get(k) or {}, v):
            return
        DB.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
        DB.commit()


def audit(actor, action, detail=""):
    ident = getattr(REQUEST_AUTH, 'identity', None) or {}
    real = ident.get('actor') or ident
    attributed = {'actor_credential': real.get('credential_id', ''),
                  'actor_label': real.get('label', ''),
                  'effective_credential': ident.get('credential_id', ''),
                  'effective_role': ident.get('role', ''),
                  'profile_id': (ident.get('impersonation') or {}).get('id', ''),
                  'impersonation_mode': (ident.get('impersonation') or {}).get('mode', '')}
    with LOCK:
        DB.execute("INSERT INTO audit(ts,actor,action,detail,actor_credential,actor_label,effective_credential,effective_role,profile_id,impersonation_mode) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), actor, action, str(detail)[:500], *attributed.values()))
        DB.commit()
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "actor": actor, "action": action, "detail": str(detail)[:2000], **attributed}) + "\n")
    except OSError:
        pass


def token_for(role, station=0):
    return hmac.new(CFG["secret"].encode(), ("%s:%s" % (role, station)).encode(), hashlib.sha256).hexdigest()[:24]


def site_token():
    return hmac.new(CFG["secret"].encode(), ("site:" + CFG["password"]).encode(), hashlib.sha256).hexdigest()[:32]


def cookie_of(headers, name="pp_site"):
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def match_secret(value, expected):
    return hmac.compare_digest(str(value).encode("utf-8"), str(expected).encode("utf-8"))


def site_ok(headers):
    """Signed in with the event password (cookie, or the X-Site header for scripts)."""
    if hosted():
        return False
    t = cookie_of(headers) or headers.get("X-Site") or ""
    return bool(t) and match_secret(t, site_token())


def named_view_key():
    if event_access.active_dataset(sys.modules[__name__]) == "live":
        return CFG["view_key"]
    return hmac.new(CFG["secret"].encode("utf-8"),
                    ("viewer:rehearsal:" + CFG["view_key"]).encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def anonymous_view_key():
    message = ("viewer:names-free:" + named_view_key()).encode("utf-8")
    return hmac.new(CFG["secret"].encode("utf-8"), message, hashlib.sha256).hexdigest()[:40]


def viewer_scope(headers, query):
    key = headers.get("X-Key") or (query.get("key") or [""])[0]
    if key and match_secret(key, anonymous_view_key()):
        return "anonymous"
    if key and match_secret(key, named_view_key()):
        return "anonymous" if query.get("names") == ["0"] else "named"
    if key:
        return None
    session = event_access.session(sys.modules[__name__], headers)
    if session:
        if session.get("dashboard") in ("organizer","scoring","home"):return "anonymous"
        return "named" if session["role"] in ("admin", "desk") and query.get("names") != ["0"] else "anonymous"
    if site_ok(headers):
        return "anonymous" if hosted() or query.get("names") == ["0"] else "named"
    return None


FAILS = {}


def throttled(ip, record=False):
    """At most 8 wrong passwords or PINs per minute from one address."""
    now = time.time()
    if len(FAILS) > 4096:
        for address in list(FAILS):
            if not FAILS[address] or now - FAILS[address][-1] >= 60:
                FAILS.pop(address, None)
        if len(FAILS) > 4096 and ip not in FAILS:
            return True
    v = [t for t in FAILS.get(ip, []) if now - t < 60]
    if record:
        v.append(now)
    FAILS[ip] = v
    return len(v) >= 8


def credential_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_role_session(role, station=0, judge=""):
    """Call with LOCK held inside the current SQLite transaction."""
    token = secrets.token_urlsafe(32)
    ttl = ORGANISER_TTL if role in ("admin", "desk") else JUDGE_TTL
    now = time.time()
    auth = event_access.auth_db(sys.modules[__name__])
    dataset = event_access.credential_dataset(sys.modules[__name__], role)
    auth.execute("DELETE FROM role_sessions WHERE expires <= ?", (now,))
    auth.execute("INSERT INTO role_sessions(digest,role,station,judge,expires,dataset) VALUES (?, ?, ?, ?, ?, ?)",
                 (credential_digest(token), role, station, judge, now + ttl, dataset))
    if auth is not DB:
        auth.commit()
    return token


def redeem_invite(station, code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", code):
        return None
    with LOCK, DB:
        invitation = DB.execute("SELECT * FROM judge_invites WHERE station=?", (station,)).fetchone()
        if not invitation or invitation["used"] or invitation["expires"] <= time.time():
            return None
        if not match_secret(credential_digest(code), invitation["digest"]):
            return None
        DB.execute("UPDATE judge_invites SET used=1 WHERE station=?", (station,))
        judge = invitation["judge"]
        token = new_role_session("judge", station, judge)
    message = ("draft:%d:%s" % (station, judge.casefold())).encode("utf-8")
    if event_access.active_dataset(sys.modules[__name__]) == "rehearsal":
        message = b"rehearsal:" + message
    draft_key = hmac.new(CFG["secret"].encode("utf-8"), message, hashlib.sha256).hexdigest()[:24]
    return {"token": token, "role": "judge", "station": station, "judge": judge, "draft_key": draft_key}


def judge_access():
    with LOCK:
        invites = {r["station"]: r for r in DB.execute("SELECT station, judge, expires, used FROM judge_invites")}
        credentials = {r['station']: r for r in event_access.auth_db(sys.modules[__name__]).execute(
            "SELECT station,label FROM access_credentials WHERE role='judge' AND active=1 AND dataset=?",
            (event_access.active_dataset(sys.modules[__name__]),))}
        active = {r["station"] for r in event_access.auth_db(sys.modules[__name__]).execute(
            "SELECT station FROM role_sessions WHERE role='judge' AND expires > ? AND dataset=?",
            (time.time(), event_access.active_dataset(sys.modules[__name__])))}
    now = time.time()
    rows = []
    for station in range(1, CFG["stations"] + 1):
        invite = invites.get(station)
        status = "Empty"
        if station in active:
            status = "Signed in"
        elif invite:
            status = "Expired" if invite["used"] or invite["expires"] <= now else "Awaiting judge"
        if station in credentials and station not in active:
            status = 'Password ready'
        rows.append({"station": station, "judge": credentials[station]['label'] if station in credentials else invite["judge"] if invite else "", "status": status})
    return rows


def change_judge_access(station, action, judge):
    code = secrets.token_urlsafe(24) if action == "issue" else None
    with LOCK, DB:
        DB.execute("DELETE FROM judge_invites WHERE station=?", (station,))
        auth = event_access.auth_db(sys.modules[__name__])
        auth.execute("DELETE FROM role_sessions WHERE role='judge' AND station=? AND dataset=?",
                     (station, event_access.active_dataset(sys.modules[__name__])))
        if auth is not DB:
            auth.commit()
        LIVE.pop(station, None)
        if code:
            DB.execute("INSERT INTO judge_invites (station, digest, judge, expires) VALUES (?, ?, ?, ?)",
                       (station, credential_digest(code), judge, time.time() + INVITE_TTL))
    audit("desk", "judge-invite-" + action, "station %d" % station)
    return {"ok": True, "code": code} if code else {"ok": True}


def who(headers):
    row = event_access.session(sys.modules[__name__], headers)
    if row:
        return (row["role"], row["station"], row["judge"])
    token = headers.get("X-Token")
    if not token or hosted():
        return None
    if match_secret(token, token_for("desk", 0)):
        return ("desk", 0, "")
    for station in range(1, CFG["stations"] + 1):
        if match_secret(token, token_for("judge", station)):
            return ("judge", station, "")
    return None


# ───────────────────────── clock and schedule ─────────────────────────
def mode():
    """'self' = each judge starts and ends their own sessions; 'desk' = one shared clock run by the scoring desk."""
    return kv_get("mode") or CFG.get("mode", "self")


def _fresh_clock():
    return {"round": 1, "running": False, "started": None, "elapsed": 0.0, "ended": False}


def clock_key(station=None):
    return "sclock:%d" % station if (station and mode() == "self") else "clock"


def clock_elapsed(clock, now):
    return clock["elapsed"] + (now - clock["started"] if clock["running"] and clock["started"] is not None else 0)


def clock_now(station=None):
    c = kv_get(clock_key(station)) or _fresh_clock()
    return c, clock_elapsed(c, time.time())


def phase_info(el):
    if el < PITCH_END:
        name = "pitch"
        step = next((i for i, e in enumerate(STEP_ENDS) if el < e), 3)
        left = STEP_ENDS[step] - el
    elif el < FEEDBACK_END:
        name, step, left = "feedback", 4, FEEDBACK_END - el
    elif el < ROUND_SECONDS:
        name, step, left = "change", 5, ROUND_SECONDS - el
    else:
        name, step, left = "over", 5, 0
    return name, step, max(0, left)


def clock_public(station=None):
    c, el = clock_now(station)
    ph, step, left = phase_info(el)
    phase_end = {"pitch": PITCH_END, "feedback": FEEDBACK_END, "change": ROUND_SECONDS, "over": 0}[ph]
    return {"kind": "round", "round": c["round"], "running": bool(c["running"]), "elapsed": round(el, 2),
            "phase_left": round(max(0, phase_end - el), 2),
            "phase": ph, "step": step, "step_left": round(left, 1), "round_left": round(max(0, ROUND_SECONDS - el), 1),
            "duration": ROUND_SECONDS, "total": ROUND_SECONDS, "started_any": bool(c["started"] or c["elapsed"]),
            "ended": bool(c.get("ended")), "mode": mode()}


def final_clock(final, now):
    c = final.get("clock") or _fresh_clock()
    elapsed = max(0, min(FINAL_SECONDS, clock_elapsed(c, now)))
    ended = bool(c.get("ended")) or elapsed >= FINAL_SECONDS
    return c, elapsed, ended


def final_clock_public(final):
    c, elapsed, ended = final_clock(final, time.time())
    left = round(FINAL_SECONDS - elapsed, 1)
    return {"kind": "final", "round": 0, "running": bool(c["running"]) and not ended, "elapsed": round(elapsed, 2),
            "phase": "over" if ended else "pitch", "step": None, "step_left": left, "round_left": left, "phase_left": left,
            "duration": FINAL_SECONDS, "total": FINAL_SECONDS, "started_any": c["started"] is not None or elapsed > 0 or ended,
            "ended": ended, "mode": mode()}


def pair_for(round_no, station):
    return event_config.pair_for(sys.modules[__name__], round_no, station)


def roster_row(pair):
    return event_config.roster_row(sys.modules[__name__], pair) if pair is not None else None


def scenario_for(station):
    for s in CONTENT["scenarios"]:
        if station in s["stations"]:
            return s
    return CONTENT["scenarios"][0]


# ───────────────────────── scoring ─────────────────────────
def sheet_dict(r):
    d = dict(r)
    d["ticks"] = json.loads(d["ticks"]); d["asked"] = json.loads(d["asked"] or "[]"); d["raised"] = json.loads(d["raised"] or "[]")
    return d


def group_counts(ticks):
    return [sum(1 for t in ticks[a:b] if t) for a, b in GROUPS]


def sheet_score(d):
    return sum(1 for t in d["ticks"] if t) * 10 + (2 if d["ai"] else 0)


def active_sheets(kind):
    with LOCK:
        rows = DB.execute("SELECT * FROM sheets WHERE kind=? AND voided=0 ORDER BY id", (kind,)).fetchall()
    return [sheet_dict(r) for r in rows]


def rank(rows):
    rows.sort(key=lambda r: (-r["total"], -r.get("wrap_rating",0), -r["solve"], -r["discover"], r["pair"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["tie"] = any(j != i and (rows[j]["total"], rows[j].get("wrap_rating",0), rows[j]["solve"], rows[j]["discover"]) == (r["total"], r.get("wrap_rating",0), r["solve"], r["discover"]) for j in range(len(rows)))
    return rows


def leaderboard():
    out = []
    for s in active_sheets("round"):
        ro = roster_row(s["pair"])
        g = group_counts(s["ticks"])
        out.append({"pair": s["pair"], "partner": ro["partner"], "p1": ro["p1"], "p2": ro["p2"], "station": s["station"],
                    "round": s["round"], "judge": s["judge"], "ticks": sum(g), "groups": g, "ai": s["ai"], "badge": ro["badge"],
                    "base": sheet_score(s), "total": sheet_score(s) + ro["badge"], "wrap_rating":s.get("wrap_rating",0), "discover": g[0], "solve": g[2],
                    "sheet": s["id"], "spoke": s["spoke"], "scenario": event_config.original_scenario(sys.modules[__name__], s["pair"])["id"], "members":ro.get("members",[]), "content_version": s.get("content_version") or ""})
    return rank(out)


def final_board():
    f = final_event.get(sys.modules[__name__])
    return final_event.rows(sys.modules[__name__], f), final_event.state(sys.modules[__name__], f)


def flags():
    out = []
    sheets = active_sheets("round")
    if not sheets:
        return out
    vers = sorted({str(s.get("content_version") or "?") for s in sheets})
    if len(vers) > 1:
        out.append({"level": "check", "text": "Sheets were scored on different content versions (%s). Check that the criteria change did not affect fairness." % ", ".join(vers)})
    scores = [sheet_score(s) for s in sheets]
    mean = sum(scores) / len(scores)
    by_j = {}
    for s in sheets:
        by_j.setdefault(s["station"], []).append(sheet_score(s))
    for st, v in by_j.items():
        if len(v) >= 2 and abs(sum(v) / len(v) - mean) >= 20:
            out.append({"level": "check", "station": st, "text": "Station %d averages %.0f against %.0f overall (%d sheets). Check calibration." % (st, sum(v) / len(v), mean, len(v))})
    for s in sheets:
        t = sum(1 for x in s["ticks"] if x)
        tag = "Station %d, pair %d: " % (s["station"], s["pair"])
        if t == 10:
            out.append({"level": "check", "station": s["station"], "sheet": s["id"], "text": tag + "all ten ticks. Confirm with the judge."})
        if t == 0:
            out.append({"level": "check", "station": s["station"], "sheet": s["id"], "text": tag + "no ticks at all. Confirm with the judge."})
        asked = {a["q"] for a in s["asked"]}
        if s["ticks"][0] and not ({"Q1", "Q2"} <= asked) and s["asked"]:
            out.append({"level": "note", "station": s["station"], "sheet": s["id"], "text": tag + "Discover tick 1 ticked, but Q1 and Q2 were not both logged."})
        if t <= 4 and not (s["notes"] or "").strip():
            out.append({"level": "note", "station": s["station"], "sheet": s["id"], "text": tag + "a low score with no notes."})
        if not s["spoke"]:
            out.append({"level": "note", "station": s["station"], "sheet": s["id"], "text": tag + "'both partners spoke' not confirmed."})
    return out


# ───────────────────────── OpenAI-compatible AI service ─────────────────────────
class GatewaySocketConnection(http.client.HTTPConnection):
    def connect(self):
        if not GATEWAY_SOCKET:
            raise OSError("KEVENOS_AI_SOCKET is not configured")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(GATEWAY_SOCKET)


def gateway_request(method, path, body=None, timeout=90):
    if (method, path) not in (("GET", "/v1/models"), ("POST", "/v1/chat/completions")):
        raise ValueError("unsupported gateway request")
    conn = GatewaySocketConnection(urlparse(GATEWAY_URL).hostname or "ai-service", timeout=timeout)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + CFG["ai_api_key"]}
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError("gateway HTTP %d" % response.status)
        content = response.read(256 * 1024 + 1)
        if len(content) > 256 * 1024:
            raise RuntimeError("gateway response too large")
        return json.loads(content)
    finally:
        conn.close()


def gateway_ready():
    if not gateway_enabled():
        return False
    with GATEWAY_HEALTH_LOCK:
        now = time.monotonic()
        if now - GATEWAY_HEALTH["t"] >= 10:
            try:
                reply = gateway_request("GET", "/v1/models", timeout=2)
                GATEWAY_HEALTH["ok"] = any(m.get("id") == GATEWAY_ROLE for m in reply["data"])
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
                GATEWAY_HEALTH["ok"] = False
            GATEWAY_HEALTH["t"] = time.monotonic()
        return GATEWAY_HEALTH["ok"]


def ai_chat(system, user, max_tokens=300, timeout=60):
    if not gateway_enabled():
        raise RuntimeError("AI is disabled")
    if not AI_SLOTS.acquire(blocking=False):
        raise RuntimeError("AI is busy")
    try:
        body = {
            "model": GATEWAY_ROLE,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_tokens": max(max_tokens, 1024),
            "reasoning_effort": "none",
        }
        reply = gateway_request("POST", "/v1/chat/completions", body, timeout=max(timeout, 90))
        text = (reply["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise RuntimeError("empty answer")
        return text
    finally:
        AI_SLOTS.release()


def tick_labels():
    return [t for g in CONTENT["rubric"] for t in g["ticks"]]


def template_feedback(ticks, guess_type=None):
    labels = tick_labels()
    got = [i for i, t in enumerate(ticks) if t]
    miss = [i for i, t in enumerate(ticks) if not t]
    worked = labels[got[0]]["short"] if got else "you stayed calm and kept going"
    # prefer the strongest step for 'worked'
    best = max(range(4), key=lambda i: (group_counts(ticks)[i] / max(1, GROUPS[i][1] - GROUPS[i][0]), -i))
    bi = [i for i in range(*GROUPS[best]) if ticks[i]]
    if bi:
        worked = labels[bi[0]]["short"]
    add = labels[miss[0]]["short"] if miss else "keep the same clear order in your next pitch"
    add_txt = ("one to add: %s." % add[0].lower() + add[1:]) if miss else "one to add: %s." % add
    return "One thing that worked: %s. %s" % (worked[0].lower() + worked[1:], add_txt[0].upper() + add_txt[1:])


def ai_feedback(d):
    ticks = d["ticks"]
    labels = tick_labels()
    sc = d.get("scenario_snapshot") or next((s for s in [*CONTENT["scenarios"], CONTENT["demo"]] if s["id"] == d.get("scenario")), None)
    done = [labels[i]["short"] for i, t in enumerate(ticks) if t]
    miss = [labels[i]["short"] for i, t in enumerate(ticks) if not t]
    asked = [a["q"] for a in d.get("asked", [])]
    core = [q["id"] for g in CONTENT["questions"] for q in g["items"] if q["core"]]
    sys_p = ("You help a judge write short spoken feedback for a sales team practising a solution device pitch. Write for readers whose "
             "English is a second language: short sentences, simple words, kind and specific. Use ONLY the facts given. "
             "Write exactly three short sentences: one thing that worked, one thing to add, one encouraging line. "
             "Maximum 55 words. No scores or numbers. No headings or bullets.")
    usr = ("Customer: %s (%s). \nWhat the team did well: %s.\nWhat was missing: %s.\nStarred questions not asked: %s.\n"
           "Judge's notes: %s" % (sc["name"] if sc else "", sc["line"] if sc else "", "; ".join(done) or "none",
                                  "; ".join(miss) or "nothing", ", ".join(q for q in core if q not in asked) or "none",
                                  (d.get("notes") or "none")[:800]))
    return ai_chat(sys_p, usr, 220, 45)


def ai_tidy(notes):
    sys_p = ("Rewrite a judge's rough notes into clear short bullet points in plain English. Keep every fact, add nothing new, "
             "keep step tags such as [Solve 6:12] at the start of a line. Maximum 120 words.")
    return ai_chat(sys_p, notes[:1500], 220, 45)


def rubric_rates():
    sheets = active_sheets("round")
    return [(label["short"], sum(1 for sheet in sheets if sheet["ticks"][i]), len(sheets))
            for i, label in enumerate(tick_labels())]


def ai_summary():
    rates = rubric_rates()
    n = rates[0][2] if rates else 0
    sys_p = ("You write a short, upbeat debrief for the room at an awards session after a sales pitch competition. Use only the "
             "data given. Plain English, short sentences. Output: 1) Three things the room did well. 2) Three things most teams "
             "missed, each with one tip. 3) One closing sentence. Maximum 220 words.")
    usr = "Teams scored: %d.\nTick rates (ticked/total):\n%s" % (n, "\n".join("- %s: %d/%d" % r for r in rates))
    return ai_chat(sys_p, usr, 420, 90), rates


# ───────────────────────── request handling ─────────────────────────
def lan_info():
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); ip = s.getsockname()[0]; s.close()
    except OSError:
        pass
    host = ""
    try:
        host = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2).stdout.strip()
        host = host + ".local" if host else ""
    except Exception:
        pass
    return ip, host


def assignment(station, final=None, final_pair=None):
    c = clock_public(station)
    f = final_event.get(sys.modules[__name__]) if final is None else final
    if final_event.engaged(f):
        p = final_event.current_pair(f)
        if final_pair is not None:
            if type(final_pair) is not int or final_pair not in f.get("visited", []):
                raise ValueError("Choose a team that has already pitched in this Final.")
            p = final_pair
        return {"kind": "final", "pair": p, "round": 0, "info": final_event.team_for(sys.modules[__name__], f, p),
                "run_id": f["run_id"], "recovery": p != final_event.current_pair(f)}
    cal = kv_get("calib") or {"active": False}
    if cal.get("active"):
        return {"kind": "calib", "pair": 0, "round": 0, "info": {"pair": 0, "partner": "Calibration: Desert Rose Travel", "p1": "", "p2": "", "badge": 0}}
    p = pair_for(c["round"], station)
    return {"kind": "round", "pair": p, "round": c["round"], "info": roster_row(p)}


def judge_state(station, final_pair=None):
    with LOCK:
        event_config.config(sys.modules[__name__])
        final = final_event.get(sys.modules[__name__])
        a = assignment(station, final, final_pair)
        mine = None
        if a["pair"] is not None:
            if a["kind"] == "final":
                r = DB.execute("SELECT * FROM sheets WHERE kind='final' AND final_run=? AND station=? AND pair=? AND voided=0 ORDER BY id DESC LIMIT 1",
                               (final["run_id"], station, a["pair"])).fetchone()
            else:
                r = DB.execute("SELECT * FROM sheets WHERE kind=? AND station=? AND voided=0 AND pair=? AND (round=? OR kind!='round') ORDER BY id DESC LIMIT 1",
                               (a["kind"], station, a["pair"], a["round"])).fetchone()
            if r:
                mine = sheet_dict(r)
                if a['kind']=='final' and mine['id'] in final.get('legacy_ballot_ids',[]):
                    mine = None
        clock = final_clock_public(final) if a["kind"] == "final" else clock_public(station)
        calibration = kv_get("calib")
        scenario = final_event.case_for(sys.modules[__name__], final, a["pair"])["id"] if a["kind"] == "final" else "MOCK" if a["kind"] == "calib" else (a.get("info") or {}).get("scenario") or scenario_for(station)["id"]
        fv = final_event.judge_view(sys.modules[__name__],station,final,a["pair"]) if a["kind"] == "final" else {"active":False,"status":final.get("status","idle")}
    nxt = a["kind"] == "round" and a["round"] < CFG["rounds"] and pair_for(a["round"] + 1, station) is not None
    return {"now": time.time(), "clock": clock, "mode": clock["mode"], "has_next": nxt, "assignment": a,
            "scenario": scenario, "scenario_snapshot": final_event.case_for(sys.modules[__name__],final,a["pair"]) if a["kind"] == "final" else None, "submitted": mine, "versions": version_info(),
            "ai": gateway_ready(), "final": fv, "calib": calibration, "program": program_now()}


def desk_state():
    # Provider and LAN discovery must not add network waits to the event lock.
    ai = gateway_ready()
    ip, host = ("", "") if hosted() else lan_info()
    with LOCK:
        return _desk_state_snapshot(ai, ip, host)


def _desk_state_snapshot(ai, ip, host):
    event_config.config(sys.modules[__name__])
    f = final_event.get(sys.modules[__name__])
    is_final = final_event.engaged(f)
    c = final_clock_public(f) if is_final else clock_public()
    stations = []
    allsheets = active_sheets("round")
    legacy_sheets = final_event.legacy_ballots(sys.modules[__name__],f) if is_final else []
    finalsheets = [row for row in final_event.sheets(sys.modules[__name__],f) if row["id"] not in f.get("legacy_ballot_ids",[])] if is_final else []
    for st in range(1, CFG["stations"] + 1):
        sc = c if is_final else clock_public(st)
        p = final_event.current_pair(f) if is_final and st in f["panel"] else None if is_final else pair_for(sc["round"], st)
        lv = LIVE.get(st, {})
        sheet = next((x for x in (finalsheets if is_final else allsheets) if x["station"] == st and x["pair"] == p and (is_final or x["round"] == sc["round"])), None) if p else None
        info = final_event.team_for(sys.modules[__name__],f,p) if is_final and p else roster_row(p)
        scenario = final_event.case_for(sys.modules[__name__],f,p)["id"] if is_final and p else (info or {}).get("scenario") or scenario_for(st)["id"]
        stations.append({"station": st, "pair": p, "info": info, "scenario": scenario, "needs_rescore":any(row["station"]==st and row["pair"]==p for row in legacy_sheets),
                         "submitted": bool(sheet), "score": sheet_score(sheet) if sheet else None, "clock": sc,
                         "ticks_live": lv.get("ticks"), "asked": lv.get("asked"), "judge": lv.get("judge") or (sheet["judge"] if sheet else ""),
                         "seen": round(time.time() - lv["t"], 1) if lv.get("t") else None})
    done = sum(1 for x in stations if x["submitted"])
    configuration = event_config.config(sys.modules[__name__])
    prelim = preliminary_review(preliminary_receipts())
    return {"preliminary": prelim, "now": time.time(), "clock": c, "mode": mode(), "stations": stations, "reveal": kv_get("reveal", "hidden"), "final": final_event.state(sys.modules[__name__],f),
            "configuration":configuration,"config":configuration,"rounds":CFG["rounds"],"judge_count":CFG["stations"],
            "calib": kv_get("calib") or {"active": False}, "public": public_mode(), "view_key": named_view_key(), "anonymous_view_key": anonymous_view_key(), "program": program_now(), "room": current_room(f, kv_get("reveal", "hidden"))[0], "agenda": [{"time": a.get("time"), "title": a.get("title"), "scene": a.get("scene", "info")} for a in agenda()], "versions": version_info(), "model": GATEWAY_ROLE if gateway_enabled() else "", "ai_enabled": gateway_enabled(), "ai": ai, "ip": ip, "host": host,
            "port": CFG["port"], "rehearsal": CFG.get("rehearsal", False), "pairs": CFG["pairs"],
            "counts": {"round": done, "all": len(allsheets), "final": len(finalsheets), "calib": len(active_sheets("calib"))}}


def public_mode():
    return kv_get("public") or CFG.get("public", "live")


SCENES = ("info", "prep", "pitch", "scores", "final", "awards", "message")


def agenda():
    return CONTENT.get("agenda", [])


def program_now():
    """Which agenda item is on screen: set by the desk, or following the clock on the host."""
    pr = kv_get("program") or {"i": 0, "since": time.time(), "mode": "manual", "announce": ""}
    items = agenda()
    if pr.get("mode") == "clock" and items:
        lt = time.localtime(time.time()); nowm = lt.tm_hour * 60 + lt.tm_min; i = 0
        for k, a in enumerate(items):
            try:
                h, m = a["time"].split(":"); 
                if int(h) * 60 + int(m) <= nowm:
                    i = k
            except (ValueError, KeyError):
                pass
        pr = dict(pr, i=i)
        # The clock-following display measures this section from its scheduled
        # start today. Manual selection retains its actual start timestamp.
        try:
            hour, minute = map(int, items[i]["time"].split(":"))
            pr['since'] = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
        except (ValueError, KeyError, OverflowError):
            pass
    if pr.get('mode', 'manual') == 'manual' and pr.get('item_id'):
        import event_commands
        selected = next((i for i, row in enumerate(event_commands._agenda_items(CONTENT)) if row['id'] == pr['item_id']), None)
        if selected is not None:
            pr = dict(pr, i=selected)
    pr["i"] = max(0, min(len(items) - 1, int(pr.get("i", 0)))) if items else 0
    return pr


def pitch_progress():
    """Where each station is in its pitch (no names), for the attendee display."""
    out = []
    sheets = active_sheets("round")
    for st in range(1, CFG["stations"] + 1):
        c = clock_public(st); p = pair_for(c["round"], st)
        if p is None:
            state = "idle"
        elif any(x["station"] == st and x["round"] == c["round"] and x["pair"] == p for x in sheets):
            state = "scored"
        elif not c["started_any"] and not c["running"]:
            state = "waiting"
        elif c.get("ended"):
            state = "feedback"
        else:
            state = {"pitch": "pitch", "feedback": "feedback", "change": "change", "over": "change"}[c["phase"]]
        out.append({"station": st, "round": c["round"], "pair": p, "state": state, "step": c["step"] if c["phase"] == "pitch" else None,
                    "left": c["step_left"] if c["running"] else None})
    return out


def attendee_pair(row, names, fields=()):
    result = {key: row[key] for key in ("pair", *fields)}
    if names:
        result.update((key, row.get(key, "")) for key in ("partner", "p1", "p2"))
        result["members"] = [{"name":m.get("name","")} for m in row.get("members",[])]
    else:
        result["partner"] = "Team %s" % row["pair"]
    return result


def room_agenda_key():
    import event_commands
    items = event_commands._agenda_items(CONTENT)
    return items[program_now()["i"]]["id"] if items else None


def clear_quiz_room():
    state = event_quiz.load(sys.modules[__name__])
    if state["hall"]:
        state.update(hall=None, rev=state["rev"] + 1)
        kv_set(event_quiz.KEY, state)


def clear_audience_room():
    state = audience_questions.load(sys.modules[__name__])
    if state.get("hall"):
        state.update(hall=None, rev=state["rev"] + 1)
        kv_set(audience_questions.KEY, state)


def clear_room_shares():
    clear_quiz_room()
    clear_audience_room()


def select_program(index):
    pr = program_now()
    pr.update(i=index, since=time.time(), mode="manual", scene="auto")
    kv_set("program", pr)
    clear_room_shares()


def program_scene(scene):
    kv_set("program", dict(program_now(), scene=scene))
    clear_room_shares()


def program_announcement(text, channel, important=False):
    pr = program_now()
    text = text.strip()
    # Give existing announcements a room copy before independently editing the phone copy.
    pr.setdefault("room_announce", pr.get("announce", ""))
    if channel in ("room", "both"):
        pr["room_announce"] = text
    if channel in ("phones", "both"):
        pr["announce"] = text
        pr["announce_updated_at"] = time.time() if text else None
    kv_set("program", pr)
    if channel in ("phones", "both"):
        event_push.announce(sys.modules[__name__], text, important, pr["announce_updated_at"])


def display_scene(final, reveal):
    pr = program_now(); items = agenda()
    override = pr.get("scene", "auto")
    current = items[pr["i"]] if items else {}
    scene = override if override in SCENES else current.get("scene", "info")
    if final_event.engaged(final) and final.get("status") != "completed" and override not in SCENES:
        scene = "final"
    return scene if scene in SCENES else "info"


def room_view(final, reveal, quiz, audience):
    pr = program_now()
    current = agenda()[pr["i"]] if agenda() else {}
    scene = display_scene(final, reveal)
    if audience:
        source, scene, title = "audience", "audience", audience["prompt"]
    elif quiz:
        source, scene = "quiz", "quiz"
        title = quiz.get("prompt") or ("Quiz champion" if quiz["kind"] == "champion" else "Quiz standings")
    else:
        source = "manual" if pr.get("scene") in SCENES else (
            "final" if final_event.engaged(final) and final.get("status") != "completed" else "agenda")
        title = current.get("title", "Programme")
    return {"scene": scene, "source": source, "title": title,
            "shares_pending": bool(event_quiz.load(sys.modules[__name__])["hall"] or
                                   audience_questions.load(sys.modules[__name__]).get("hall"))}


def current_room(final, reveal, quiz_state=None, names=True):
    app = sys.modules[__name__]
    quiz, audience = event_quiz.hall(app, quiz_state, names=names), audience_questions.hall(app)
    return room_view(final, reveal, quiz, audience), quiz, audience


def revealed_podium(final, reveal, names):
    if reveal == "winners":
        source = final.get("published_rows", []) if final.get("status") == "completed" else []
    elif reveal == "top3":
        source = leaderboard()[:3]
    else:
        source = []
    return [attendee_pair(row, names, ("rank", "total")) for row in source]


def public_state(names=True, presenter=False):
    # Build one coherent programme/score snapshot instead of repeatedly joining
    # the lock queue while a burst of answers is being committed.
    with LOCK:
        return _public_state_snapshot(names, presenter)


def _public_state_snapshot(names, presenter=False):
    """Audience-safe state: no private answers, notes, ballots or partial Final results."""
    final = final_event.get(sys.modules[__name__])
    reveal = kv_get("reveal", "hidden")
    if reveal == "winners" and final.get("status") != "completed": reveal = "hidden"
    base = {"now": time.time(), "event": CONTENT["event"]["name"], "versions": {"app": APP_VERSION, "content": CONTENT.get("content_version", 1)},
            "pairs": CFG["pairs"], "rehearsal": CFG.get("rehearsal", False), "configuration":event_config.config(sys.modules[__name__])}
    pr = program_now(); items = agenda()
    base["program"] = {"index": pr["i"], "mode": pr.get("mode", "manual"), "since": pr.get("since"), "announce": pr.get("room_announce", pr.get("announce", "")), "total": len(items),
                       "items": [{"time": a.get("time"), "title": a.get("title"), "line": a.get("line", ""), "do": a.get("do", []), "min": a.get("min", 0), "who": a.get("who", "")} for a in items]}
    quiz_state = None
    if presenter:
        app = sys.modules[__name__]
        quiz_state = event_quiz.load(app)
        event_quiz.expire(quiz_state, event_quiz.now(app))
        base["quiz_leaderboard"] = event_quiz.room_standings(quiz_state, names)
    base["room"], base["quiz"], base["audience"] = current_room(final, reveal, quiz_state, names=names)
    base["scene"] = display_scene(final,reveal); base["scene_override"] = pr.get("scene", "auto")
    base["clock"] = final_clock_public(final) if final_event.engaged(final) else clock_public()
    base["final"] = final_event.public_view(sys.modules[__name__],final,names)
    base["reveal"] = reveal
    if reveal in ("top3", "winners"): base["podium"] = revealed_podium(final,reveal,names)
    base["steps"] = [{"n": x["n"], "name": x["name"], "minutes": x["minutes"], "line": x["line"]} for x in CONTENT.get("steps", [])]
    base["bells"] = CONTENT.get("bells", [])
    base["stations"] = pitch_progress() if not final_event.engaged(final) else []
    base["mode"] = mode()
    lb = leaderboard()
    if public_mode() != "live": return dict(base, off=True, rows=[], scored=len(lb))
    audience = [attendee_pair(row, names, ("rank", "total", "ticks", "groups", "round")) for row in lb]
    return dict(base, off=False, rows=audience, scored=len(lb))


def board_state(names=True):
    configuration=event_config.config(sys.modules[__name__])
    c = clock_public(); final = final_event.get(sys.modules[__name__])
    reveal = kv_get("reveal", "hidden")
    if reveal == "winners" and final.get("status") != "completed": reveal = "hidden"
    is_final=final_event.engaged(final)
    sheets = active_sheets("round")
    out = {"now": time.time(), "clock": final_clock_public(final) if is_final else c, "mode": mode(),
           "reveal": reveal, "scene":display_scene(final,reveal), "event": CONTENT["event"]["name"], "configuration":configuration,
           "round_done": len([x for x in sheets if x["round"] == c["round"]]),
           "stations": CFG["stations"], "all_done": len(sheets), "pairs": CFG["pairs"],
           "final": final_event.public_view(sys.modules[__name__],final,names) if is_final else None}
    if mode() == "self" and not is_final:
        clocks = []
        for st in range(1, CFG["stations"] + 1):
            sc = clock_public(st); p = pair_for(sc["round"], st)
            clocks.append({"station": st, "round": sc["round"], "pair": p, "clock": sc,
                         "submitted": bool(p and any(x["station"] == st and x["round"] == sc["round"] and x["pair"] == p for x in sheets))})
        out["station_clocks"] = clocks
    if reveal in ("top3", "winners"): out["podium"] = revealed_podium(final,reveal,names)
    return out


# ───────────────────────── content (editable without a rebuild) ─────────────────────────
def content_path():
    return os.path.join(DATA, "content.json")


def load_content(db=None, data=None):
    """The edited copy in the data folder wins over the content.json baked into the app."""
    selected_db = db or DB
    if database.is_server(selected_db):
        row = selected_db.execute("SELECT v FROM kv WHERE k='content_current'").fetchone()
        if row:
            c = json.loads(row[0])
            c.setdefault("content_version", 1)
            c["_source"] = "database"
            return c
        c = bundled_content()
        c.setdefault("content_version", 1)
        c["_source"] = "bundled"
        return c
    p = os.path.join(data or DATA, "content.json")
    src = p if os.path.exists(p) else os.path.join(HERE, "content.json")
    with open(src, encoding="utf-8") as source:
        c = json.load(source)
    c.setdefault("content_version", 1)
    if src == p:
        # Preserve edited text and carry scene defaults forward for known schedule entries.
        scenes = {(a["time"], a["title"]): a["scene"] for a in bundled_content().get("agenda", []) if "scene" in a}
        for item in c.get("agenda", []):
            item.setdefault("scene", scenes.get((item.get("time"), item.get("title")), "info"))
    c["_source"] = "edited" if src == p else "bundled"
    return c


def bundled_content():
    with open(os.path.join(HERE, "content.json"), encoding="utf-8") as f:
        c = json.load(f)
    return c


def version_info():
    return {"app": APP_VERSION, "schema": SCHEMA_VERSION, "content": CONTENT.get("content_version", 1), "content_source": CONTENT.get("_source", "bundled"),
            "content_saved": CONTENT.get("content_saved"), "content_note": CONTENT.get("content_note", "")}


def tick_ids(c):
    return [t["id"] for g in c.get("rubric", []) for t in g.get("ticks", [])]


def validate_content(new):
    """Text can change freely; the shape the scoring depends on cannot. Returns a list of problems."""
    base = bundled_content(); errs = []
    if not isinstance(new, dict):
        return ["The content must be a JSON object."]
    for k in base:
        if k not in new:
            errs.append("Missing section: %s" % k)
    if errs:
        return errs
    try:
        if [len(g["ticks"]) for g in new["rubric"]] != [len(g["ticks"]) for g in base["rubric"]]:
            errs.append("Criteria: keep %s ticks per step, as scoring depends on it." % "/".join(str(len(g["ticks"])) for g in base["rubric"]))
        if tick_ids(new) != tick_ids(base):
            errs.append("Criteria: tick ids must stay %s." % ", ".join(tick_ids(base)))
        for g in new["rubric"]:
            for t in g["ticks"]:
                for f in ("text", "counts", "short"):
                    if not str(t.get(f, "")).strip():
                        errs.append("Criteria %s: '%s' is empty." % (t["id"], f))
        if len(new["steps"]) != len(base["steps"]):
            errs.append("Steps: keep %d steps." % len(base["steps"]))
        if [s["id"] for s in new["scenarios"]] != [s["id"] for s in base["scenarios"]] or [s["stations"] for s in new["scenarios"]] != [s["stations"] for s in base["scenarios"]]:
            errs.append("Scenarios: keep the same ids and station assignments.")
        qids = [q["id"] for g in new["questions"] for q in g["items"]]
        if len(qids) != len(set(qids)):
            errs.append("Questions: ids must be unique.")
        if not {"Q1", "Q2", "Q3", "Q13"} <= set(qids):
            errs.append("Questions: Q1, Q2, Q3 and Q13 must stay (they suggest Discover ticks).")
        if set(new["mock"]["key"]) != set(base["mock"]["key"]):
            errs.append("Mock pitch: the answer key must cover the same ticks.")
        for k in ("bonus_tick",):
            if not str(new[k].get("text", "")).strip():
                errs.append("%s: text is empty." % k)
    except (KeyError, TypeError, AttributeError) as e:
        errs.append("Structure problem: %s" % e)
    return errs


def content_version(version):
    if version == CONTENT.get("content_version", 1):
        return CONTENT
    if database.is_server(DB):
        row = DB.execute("SELECT v FROM kv WHERE k=?", ("content_history:%d" % version,)).fetchone()
        if row:
            return json.loads(row[0])
        if version == 1:
            return bundled_content()
        raise FileNotFoundError("The content version was not found.")
    path = os.path.join(DATA, "content-history", "content-v%d.json" % version)
    if version == 1 and not os.path.isfile(path):
        return bundled_content()
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def refresh_content():
    """Pick up an edit committed by another server process."""
    global CONTENT
    if not database.is_server(DB):
        return CONTENT
    latest = load_content(DB, DATA)
    if latest.get("content_version", 1) != CONTENT.get("content_version", 1):
        CONTENT = latest
    return CONTENT


def save_content(new, note, actor="desk"):
    global CONTENT
    new = {k: v for k, v in new.items() if not k.startswith("_")}
    expected = int(CONTENT.get("content_version", 1))
    new["content_saved"] = time.time(); new["content_note"] = str(note or "")[:300]
    if database.is_server(DB):
        with database.named_lock(DB, "content"), DB:
            current = load_content(DB, DATA)
            if int(current.get("content_version", 1)) != expected:
                CONTENT = current
                raise ValueError("The event content changed. Reload the latest version before saving.")
            new["content_version"] = expected + 1
            txt = json.dumps(new, ensure_ascii=False, separators=(",", ":"))
            DB.execute("INSERT INTO kv(k,v) VALUES('content_current',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (txt,))
            DB.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                       ("content_history:%d" % new["content_version"], txt))
        CONTENT = load_content(DB, DATA)
        audit(actor, "content", "v%d %s" % (new["content_version"], new["content_note"]))
        return new["content_version"]
    new["content_version"] = expected + 1
    os.makedirs(os.path.join(DATA, "content-history"), exist_ok=True)
    txt = json.dumps(new, ensure_ascii=False, indent=1)
    tmp = content_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as target:
        target.write(txt)
    os.replace(tmp, content_path())
    history_path = os.path.join(DATA, "content-history", "content-v%d.json" % new["content_version"])
    with open(history_path, "w", encoding="utf-8") as target:
        target.write(txt)
    CONTENT = load_content()
    audit(actor, "content", "v%d %s" % (new["content_version"], new["content_note"]))
    return new["content_version"]


def history_list():
    out = []
    if database.is_server(DB):
        for row in DB.execute("SELECT k,v FROM kv WHERE k LIKE 'content_history:%'"):
            try:
                version = int(row["k"].split(":", 1)[1])
                value = json.loads(row["v"])
                out.append({"version": version, "saved": value.get("content_saved"), "note": value.get("content_note", "")})
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        return sorted(out, key=lambda x: -x["version"])
    d = os.path.join(DATA, "content-history")
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            m = re.match(r"content-v(\d+)\.json$", f)
            if m:
                try:
                    j = json.load(open(os.path.join(d, f), encoding="utf-8"))
                    out.append({"version": int(m.group(1)), "saved": j.get("content_saved"), "note": j.get("content_note", "")})
                except ValueError:
                    pass
    return sorted(out, key=lambda x: -x["version"])


def save_sheet(role_station, body, actor):
    kind = body.get("kind", "round"); station = int(role_station)
    with LOCK:
        receipt = body.get('submission_id')
        receipt_key_name = None
        if receipt is not None:
            if not isinstance(receipt, str) or not re.fullmatch(r'[a-f0-9-]{36}', receipt):
                return {'error': 'Invalid submission reference.'}, 400
            identity = getattr(REQUEST_AUTH, 'identity', None) or {}
            owner = identity.get('credential_id') or actor
            scope = f'{station}:{owner}:{receipt}'
            receipt_key_name = 'judge_receipt:' + hashlib.sha256(scope.encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            previous = kv_get(receipt_key_name)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    return {'error': 'This submission reference already belongs to a different sheet.'}, 409
                return previous['result'], 200
        final = final_event.get(sys.modules[__name__])
        a = assignment(station,final)
        if kind == "final":
            error = final_event.before_submit(sys.modules[__name__],final,station,body)
            if error:return error
            final_rating=body.get('final_rating')
            if type(final_rating) is not int or not 1<=final_rating<=5:
                return {"error":"Choose a Final score from one to five stars."},400
            wrap_rating=0
            pair = body["pair"]; run_id=final["run_id"]; round_no=0
        else:
            pair = body.get('pair')
            if type(pair) is not int:
                return {'error': 'Choose the team on your assigned sheet.'}, 400
            round_no = body.get('round', a['round']) if kind == 'round' else 0
            run_id = ''
            if kind == 'round' and type(round_no) is not int:
                return {'error': 'Choose the round on your assigned sheet.'}, 400
            if (kind, pair, round_no) != (a['kind'], a['pair'], a['round']):
                # A late first sheet can fill the exact waived assignment without
                # moving the station. Qualifier results freeze when the Final starts.
                key = receipt_key({'round': round_no, 'station': station, 'pair': pair})
                can_recover = (kind == a['kind'] == 'round' and bool(receipt)
                               and round_no < a['round'] and pair_for(round_no, station) == pair
                               and key in kv_get('preliminary_exceptions', {})
                               and not DB.execute("SELECT 1 FROM sheets WHERE kind='round' AND station=? AND pair=? AND round=? AND voided=0",
                                                  (station, pair, round_no)).fetchone())
                if not can_recover:
                    return {'error': 'The Desk has moved on. Keep your saved sheet and contact the Scoring Desk.'}, 409
            final_rating=0
            wrap_rating=body.get('wrap_rating',0)
            if kind=='round' and (type(wrap_rating) is not int or not 1<=wrap_rating<=5):
                return {"error":"Choose an overall impression and confidence rating from one to five stars."},400
        ticks = [1 if x else 0 for x in body.get("ticks", [])][:10]
        ticks += [0] * (10 - len(ticks))
        with DB:
            DB.execute("UPDATE sheets SET voided=1 WHERE kind=? AND station=? AND pair=? AND voided=0 AND round=? AND final_run=?",
                       (kind, station, pair, round_no, run_id))
            cur = DB.execute("INSERT INTO sheets(kind,pair,station,round,judge,ticks,ai,spoke,notes,feedback,asked,raised,created,app_version,content_version,final_run,wrap_rating,final_rating) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (kind, pair, station, round_no, str(body.get("judge", ""))[:60], json.dumps(ticks),
                          1 if body.get("ai") else 0, 1 if body.get("spoke") else 0, str(body.get("notes", ""))[:4000],
                          str(body.get("feedback", ""))[:2000], json.dumps(body.get("asked", [])[:60]),
                          json.dumps(body.get("raised", [])[:20]), time.time(), APP_VERSION, str(CONTENT.get("content_version", 1)),run_id,wrap_rating,final_rating))
            sid = cur.lastrowid
            if kind == 'round' and body.get('participant_feedback_approved') is True and str(body.get('feedback', ''))[:2000].strip():
                DB.execute('UPDATE sheets SET participant_feedback_approved=1 WHERE id=?', (sid,))
            result = {"ok": True, "id": sid, "score": final_rating if kind == 'final' else sum(ticks) * 10 + (2 if body.get("ai") else 0), "run_id": run_id}
            if receipt_key_name:
                DB.execute('INSERT INTO kv(k,v) VALUES(?,?)',
                           (receipt_key_name, json.dumps({'fingerprint': fingerprint, 'result': result})))
        if kind=="final":final_event.invalidate_decision(sys.modules[__name__],final)
        audit(actor, "submit", "sheet %d %s team %s judge %d ticks %d" % (sid, kind, pair, station, sum(ticks)))
        return result, 200


def preliminary_receipts():
    """One status per configured assignment. Empty capacity is marked separately."""
    return [dict(row) for row in DB.execute("""
        SELECT a.pair, a.station, a.round_no AS round,
          EXISTS(SELECT 1 FROM roster r WHERE r.pair=a.pair) AS registered,
          EXISTS(SELECT 1 FROM sheets sh WHERE sh.kind='round' AND sh.voided=0
            AND sh.pair=a.pair AND sh.station=a.station AND sh.round=a.round_no) AS accepted
        FROM team_assignments a WHERE a.pair<=? ORDER BY a.round_no,a.station
    """, (CFG['pairs'],))]


def preliminary_review(rows):
    exceptions = kv_get('preliminary_exceptions', {})
    return [dict(row, exception=exceptions.get(receipt_key(row))) for row in rows]


def receipt_key(row):
    return f"{row['round']}:{row['station']}:{row['pair']}"


def review_preliminary(rows, body, operation, scope, actor):
    """Return pending exception writes or a conflict for the exact operation."""
    missing = [row for row in preliminary_review(rows) if not row['accepted'] and not row['exception']]
    if not missing:
        if body.get('review_token'):
            return {}, ({'error': 'The score sheet status changed. Review the latest state before continuing.'}, 409)
        return {}, None
    token = hashlib.sha256(json.dumps([operation, scope, missing], sort_keys=True).encode()).hexdigest()
    issue = {'error': 'Wait for the required score sheets, or record a Desk exception.',
             'code': 'preliminary_sheets_missing', 'missing': missing, 'review_token': token}
    reason = body.get('override_reason')
    if reason is None:
        return {}, (issue, 409)
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 500:
        return {}, ({'error': 'Give a reason of 8–500 characters for continuing with missing sheets.'}, 400)
    if body.get('review_token') != token:
        return {}, ({**issue, 'error': 'The round or outstanding sheets changed. Review the current exception before continuing.'}, 409)
    exceptions = kv_get('preliminary_exceptions', {})
    for row in missing:
        exceptions[receipt_key(row)] = {'reason': reason.strip(), 'actor': actor, 'at': time.time()}
    return {'preliminary_exceptions': exceptions}, None


def write_judging_state(values):
    with DB:
        DB.executemany('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                       [(key, json.dumps(value)) for key, value in values.items()])


def preliminary_clock_command(body, actor):
    action, station = body.get('action'), body.get('station')
    if station is not None and (type(station) is not int or not 1 <= station <= CFG['stations'] or mode() != 'self'):
        return {'error': 'Choose a configured station in judge-paced mode.'}, 400
    if action not in ('start', 'pause', 'reset', 'feedback', 'finish', 'next', 'goto', 'seek'):
        return {'error': 'Choose a clock action.'}, 400
    if action == 'goto' and (type(body.get('round')) is not int or not 1 <= body['round'] <= CFG['rounds']):
        return {'error': 'Choose a round within the configured schedule.'}, 400
    if action == 'seek':
        elapsed = body.get('elapsed')
        if type(elapsed) not in (int, float) or not 0 <= elapsed <= ROUND_SECONDS:
            return {'error': 'Choose a clock position within this round.'}, 400
    event_config.config(sys.modules[__name__])
    targets = [station] if station is not None else list(range(1, CFG['stations'] + 1)) if mode() == 'self' else [None]
    clocks = {st: clock_now(st) for st in targets}
    expected = body.get('expected_round')
    if expected is not None and (type(expected) is not int or any(raw['round'] != expected for raw, _ in clocks.values())):
        return {'error': 'The round changed. Review the current assignment before using the clock.'}, 409
    changes = {}
    if action in ('next', 'goto'):
        destinations = {st: body['round'] if action == 'goto' else min(CFG['rounds'], raw['round'] + 1)
                        for st, (raw, _) in clocks.items()}
        moving = {st: (raw['round'], destinations[st]) for st, (raw, _) in clocks.items()
                  if raw['round'] != destinations[st]}
        if any(raw['running'] and elapsed < FEEDBACK_END for st, (raw, elapsed) in clocks.items() if st in moving):
            return {'error': 'Pause or finish any running pitch or feedback before switching rounds.'}, 409
        rows = []
        for row in preliminary_receipts():
            move = moving.get(None) if mode() == 'desk' else moving.get(row['station'])
            if move and (row['round'] == move[0] or move[0] < row['round'] < move[1]):
                rows.append(row)
        scope = [[st, raw, destinations[st]] for st, (raw, _) in clocks.items()]
        changes, error = review_preliminary(rows, body, action, scope, actor)
        if error:
            return error
        clocks = {st: values for st, values in clocks.items() if st in moving}
    for st, (raw, elapsed) in clocks.items():
        if action != 'start' or not raw['running']:
            changes[clock_key(st)] = updated_clock(raw, elapsed, action, body)
    write_judging_state(changes)
    if 'preliminary_exceptions' in changes:
        audit(actor, 'preliminary-exception', f"{action}: {body['override_reason'].strip()}")
    if changes:
        audit(actor, 'clock', f'{action}' + (f' station {station}' if station is not None else ''))
    return {'clocks': [dict(clock_public(st), station=st) for st in targets]}, 200


def judging_mode_command(body, actor):
    new_mode = body.get('mode')
    if new_mode not in ('self', 'desk'):
        return {'error': 'Choose the Desk or judge-paced clock.'}, 400
    if new_mode == mode():
        return {'mode': new_mode}, 200
    if final_event.engaged(final_event.get(sys.modules[__name__])):
        return {'error': 'End the Final before changing the round clock mode.'}, 409
    stations = range(1, CFG['stations'] + 1)
    clocks = [clock_now(st)[0] for st in (stations if new_mode == 'desk' else [None])]
    if any(raw['running'] for raw in clocks):
        return {'error': 'Pause the running clocks before changing clock mode.'}, 409
    if len({raw['round'] for raw in clocks}) != 1:
        return {'error': 'Bring all stations to the same round before switching to a shared clock.'}, 409
    clock = dict(clocks[0])
    if len({(raw['elapsed'], bool(raw.get('ended'))) for raw in clocks}) > 1:
        if not all(raw.get('ended') for raw in clocks):
            return {'error': 'Station clocks differ. End each session on this round before sharing one clock.',
                    'code': 'station_clocks_differ'}, 409
        clock['elapsed'] = max(raw['elapsed'] for raw in clocks)
    values = {'mode': new_mode}
    if new_mode == 'self':
        values.update((f'sclock:{st}', clock) for st in stations)
    else:
        values['clock'] = clock
    write_judging_state(values)
    audit(actor, 'mode', new_mode)
    return {'mode': new_mode}, 200


def clock_action(station, a, b):
    """One clock command for the shared clock (station=None) or for one station's own clock (self mode)."""
    clock, elapsed = clock_now(station)
    kv_set(clock_key(station), updated_clock(clock, elapsed, a, b))


def updated_clock(clock, el, a, b):
    c = dict(clock)
    now, rounds = time.time(), CFG["rounds"]
    if a == "start":
        c.update(running=True, started=now, ended=False)
    elif a == "pause":
        c.update(running=False, elapsed=el, started=None)
    elif a == "reset":
        c.update(running=False, elapsed=0.0, started=None, ended=False)
    elif a == "feedback":       # end the pitch now and start the feedback time
        el = max(el, PITCH_END)
        c.update(elapsed=el, started=now if c["running"] else None, ended=False)
    elif a == "finish":         # session over: stop the clock
        c.update(running=False, elapsed=el, started=None, ended=True)
    elif a == "next":
        c.update(round=min(rounds, c["round"] + 1), running=False, elapsed=0.0, started=None, ended=False)
    elif a == "goto":
        c.update(round=max(1, min(rounds, int(b.get("round", 1)))), running=False, elapsed=0.0, started=None, ended=False)
    elif a == "seek":
        c.update(elapsed=float(b.get("elapsed", 0)), started=now if c["running"] else None)
    return c


def final_clock_action(final, action, body):
    """Apply a Desk clock action without writing to any round clock."""
    if final.get("status") in ("review", "completed"):
        return {"error":"Final scoring is complete. Review results or reopen them for corrections."},409
    now = time.time()
    c, elapsed, ended = final_clock(final, now)
    c = dict(c, running=bool(c["running"]) and not ended, elapsed=elapsed, started=None, ended=ended)
    if action == "start":
        c["running"] = not ended
    elif action == "pause":
        c["running"] = False
    elif action == "reset":
        c = _fresh_clock()
    elif action == "finish":
        c.update(running=False, ended=True)
    elif action == "seek":
        try:
            elapsed = float(body.get("elapsed", 0))
        except (ValueError, TypeError):
            return {"error": "Enter a valid final clock time."}, 400
        if not 0 <= elapsed <= FINAL_SECONDS:
            return {"error": "Final clock time must be within eight minutes."}, 400
        c.update(elapsed=elapsed, running=c["running"] and elapsed < FINAL_SECONDS, ended=elapsed >= FINAL_SECONDS)
    else:
        return {"error": "Use the Final controls to select a finalist."}, 409
    if c["running"]:
        c["started"] = now
    final["clock"] = c
    final["status"] = "collecting" if c.get("ended") else "pitch" if c["running"] else "ready"
    final["active"] = True
    kv_set("final", final)
    audit("desk", "final-clock", action)
    return final_clock_public(final), 200


def final_action(body):
    return final_event.action(sys.modules[__name__],body)


def judge_session(station, b):
    """A judge runs their own station in self mode: start, pause, jump to feedback, finish, next pair."""
    if mode() != "self":
        return {"error": "The admin runs the clock in this round. Wait for the bell."}, 409
    a = assignment(station)
    if a["kind"] != "round":
        return {"error": "The desk is running the %s. Wait for it." % a["kind"]}, 409
    act = b.get("action")
    if b.get('expected_round', a['round']) != a['round']:
        return {'error': 'Your station moved to another round. Review the assignment.'}, 409
    if act not in ("start", "pause", "feedback", "finish", "next", "reset"):
        return {"error": "unknown action"}, 400
    if act == "next":
        if a["pair"] is None or pair_for(a["round"] + 1, station) is None:
            return {"error": "There is no later pair for your station."}, 409
        with LOCK:
            r = DB.execute("SELECT id FROM sheets WHERE kind='round' AND station=? AND pair=? AND round=? AND voided=0", (station, a["pair"], a["round"])).fetchone()
        if not r and (hosted() or not b.get("force")):
            return {"error": "Submit the sheet for team %d before moving on. Contact the Scoring Desk if you need help." % a["pair"],
                    "needs_force": not hosted()}, 409
        raw, elapsed = clock_now(station)
        if raw['running'] and elapsed < FEEDBACK_END and hosted():
            return {"error": "Finish feedback or end the session before moving to the next team."}, 409
    if act == "reset" and not b.get("force"):
        return {"error": "Confirm to reset your clock.", "needs_force": True}, 409
    clock_action(station, act, b)
    audit("judge%d" % station, "session-" + act, "pair %s round %s" % (a["pair"], a["round"]))
    return {"ok": True, "clock": clock_public(station)}, 200


def parse_roster(text):
    rows = list(csv.reader(io.StringIO(text.strip())))
    headers = None
    if rows and rows[0] and rows[0][0].strip().lower() in ("pair", "team", "#", "no"):
        headers = [key.strip().lower() for key in rows.pop(0)]
    out = []
    flag = lambda value: int(value.strip().lower() in ("1", "y", "yes", "true", "succeed"))
    for raw in rows:
        row = [value.strip() for value in raw]
        if not any(row):
            continue
        try:
            pair = int(row[0])
        except (ValueError, IndexError):
            raise ValueError("Every CSV row needs a valid team number.")
        if headers:
            fields = dict(zip(headers, row))
            result = {"pair":pair,"partner":fields.get("partner",fields.get("company",""))}
            slots = sorted({int(key[1:]) for key in headers if re.fullmatch(r"p[1-9][0-9]*",key)})
            capacity = event_config.member_capacity(event_config.config(sys.modules[__name__]),pair)
            members = []
            for i in slots:
                name = fields.get("p"+str(i),"")
                coach = flag(fields.get("coach"+str(i),fields.get("c"+str(i),"")))
                if i>capacity and not name and not coach:
                    continue
                members.append({"id":i,"name":name,"coach":coach})
            if slots:
                result["members"] = members
        else:
            row += [""] * 6
            result = dict(pair=pair,partner=row[1],p1=row[2],p2=row[3],coach1=flag(row[4]),coach2=flag(row[5]))
        out.append(result)
    return out


def csv_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))
                                   or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def cards_html():
    labels = tick_labels()
    lb = {r["pair"]: r for r in leaderboard()}
    sheets = {s["pair"]: s for s in active_sheets("round")}
    cards = []
    for p in range(1, CFG["pairs"] + 1):
        ro = roster_row(p); s = sheets.get(p)
        if not s:
            continue
        marks = "".join("<li class='%s'>%s %s</li>" % ("y" if t else "n", "&#10003;" if t else "&#9675;", html.escape(labels[i]["short"])) for i, t in enumerate(s["ticks"]))
        cards.append("<section><h2>%s</h2><p class='m'>%s</p><ul>%s</ul><h3>Your judge said</h3><p>%s</p><p class='f'>Team %d &middot; Round %d &middot; Station %d</p></section>" % (
            html.escape(ro["partner"]), html.escape(" and ".join(m["name"] for m in ro["members"] if m["name"])), marks,
            html.escape(s["feedback"] or "").replace("\n", "<br>"), p, s["round"], s["station"]))
    return ("<!doctype html><meta charset=utf-8><title>Feedback cards</title><style>body{font:14px -apple-system,Helvetica,sans-serif;margin:0}"
            "section{width:190mm;height:130mm;box-sizing:border-box;padding:12mm;page-break-inside:avoid;border-bottom:1px dashed #bbb;overflow:hidden}"
            "h2{margin:0 0 2mm;font-size:20px}h3{margin:4mm 0 1mm;font-size:13px;text-transform:uppercase;color:#666}ul{list-style:none;padding:0;margin:3mm 0;columns:2}"
            "li{margin:1mm 0}li.y{color:#0a7d33}li.n{color:#999}.m{color:#555;margin:0}.f{color:#888;font-size:11px}"
            "@media print{section{border:0;height:128mm}}</style>" + "".join(cards))


class EventHTTPServer(ThreadingHTTPServer):
    # Quiz release and QR sign-in can open many connections simultaneously.
    # This bounds pending TCP admission; handlers and auth rules stay unchanged.
    request_queue_size = 256


class Handler(BaseHTTPRequestHandler):
    def redirect_legacy_host(self):
        return False

    server_version = "KevenOS/1.0"

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            target = getattr(self, "path", "").partition("?")[0]
            sys.stderr.write("%s %s %s\n" % (self.address_string(), self.command or "", target))

    # ---- helpers ----
    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        visitor_cookie = event_access.track_response(sys.modules[__name__], self, code)
        ident = getattr(self, '_request_identity', None) or {}
        if self.command == 'POST' and 200 <= code < 300 and ident.get('impersonation') and ident['impersonation']['mode'] == 'support':
            audit('admin', 'profile-operation', urlparse(self.path).path)
        self.send_response(code)
        if visitor_cookie:
            self.send_header("Set-Cookie", visitor_cookie)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if WORKSPACE:
            self.send_header("X-Event-Context", WORKSPACE.context)
        identity = event_access.session(sys.modules[__name__], self.headers)
        access_context = getattr(self, '_response_access_context', (identity or {}).get('context', ''))
        if access_context:
            self.send_header('X-Access-Context', access_context)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= 400000:
            raise ValueError("too large")
        raw = self.rfile.read(n) if n else b""
        body = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number"))) if raw else {}
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        return body

    def page(self, name):
        p = os.path.join(STATIC, name)
        if not os.path.isfile(p):
            return self.send(404, {"error": "not found"})
        ct = {"html": "text/html", "css": "text/css", "js": "application/javascript", "png": "image/png", "webmanifest": "application/manifest+json"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        with open(p, "rb") as f:
            data = f.read()
        if hosted() and ct == 'text/html':
            data = re.sub(br"<body(?=[ >])", b'<body data-scoped="1"', data, count=1)
        if ct == "text/html" and WORKSPACE:
            attrs = ('<body data-event-context="%s" data-event-mode="%s"' % (WORKSPACE.context, WORKSPACE.mode)).encode()
            data = re.sub(br"<body(?=[ >])", attrs, data, count=1)
        self.send(200, data, ct)

    def registration_video(self):
        path = os.path.join(STATIC, 'registration-loop-v2.mp4')
        if not os.path.isfile(path):
            return self.send(404, {'error': 'Registration video unavailable.'})
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        requested = self.headers.get('Range', '')
        if requested:
            match = re.fullmatch(r'bytes=([0-9]{0,18})-([0-9]{0,18})', requested)
            if match and match[1]:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            elif match and match[2]:
                start = max(0, size - int(match[2]))
            else:
                start = size
            if start > end:
                return self.send(416, {'error': 'Requested byte range is unavailable.'},
                                 extra={'Content-Range': f'bytes */{size}'})
            status = 206
        self.send_response(status)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with open(path, 'rb') as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = source.read(min(remaining, 128*1024))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- routing ----
    def startup_response(self):
        if not STARTING:
            return False
        if urlparse(self.path).path == '/healthz' and READY.wait(timeout=120):
            return False
        data = b'{"ok":false,"status":"initializing"}'
        self.send_response(503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Retry-After', '2')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)
        return True

    def do_GET(self):
        if self.startup_response():
            return
        try:
            if WORKSPACE:
                WORKSPACE.sync()
            if WORKSPACE and WORKSPACE.guard.blocked:
                return self.send(503, {'error':'Event recovery is required. Contact the event administrator.'})
            return self._guarded_GET()
        except RuntimeError:
            if WORKSPACE and WORKSPACE.guard.blocked:
                return self.send(503, {'error':'Event recovery is required. Contact the event administrator.'})
            raise

    def _guarded_GET(self):
        if self.redirect_legacy_host():
            return
        if agent_api.handle(sys.modules[__name__], self) or agent_api.proxy_mcp(sys.modules[__name__], self):
            return
        with WORKSPACE.read() if WORKSPACE else nullcontext():
            try:
                refresh_content()
                self._dispatch_GET()
            finally:
                REQUEST_AUTH.identity = None

    def _dispatch_GET(self):
        try:
            if self.redirect_legacy_host():
                return
            u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
            if path == "/healthz":
                return self.send(200, {"ok": True})
            if path in ('/participant-sw.js', '/participant.webmanifest'):
                return self.page(path[1:])
            if event_profiles.guard(sys.modules[__name__], self, path):
                return
            authed = site_ok(self.headers); viewer = viewer_scope(self.headers, q)
            if event_operations.handle(sys.modules[__name__], self, path):
                return
            if event_assistant.handle(sys.modules[__name__], self, path):
                return
            if event_roles.handle(sys.modules[__name__], self, path):
                return
            if event_profiles.handle_get(sys.modules[__name__], self, path):
                return
            if event_config.handle_get(sys.modules[__name__], self, path, q):
                return
            if WORKSPACE and event_workspace.handle_get(sys.modules[__name__], self, path, q):
                return
            if event_mail.handle_get(sys.modules[__name__], self, path, q):
                return
            if event_quiz.handle(sys.modules[__name__], self, path):
                return
            if audience_questions.handle(sys.modules[__name__], self, path):
                return
            if event_push.handle(sys.modules[__name__], self, path):
                return
            if participant_workspace.handle(sys.modules[__name__], self, path):
                return
            if registration_desk.handle(sys.modules[__name__], self, path):
                return
            if event_access.handle_get(sys.modules[__name__], self, path, q):
                return
            if path == "/event":
                return self.page("guest.html")
            if path == "/home":
                return self.page("home.html")
            if path in ("/organizer", "/scoring"):
                return self.page("desk.html")
            if path in ("/", "/index.html"):
                return self.page("index.html" if hosted() or authed else "gate.html")
            if path in ("/guest", "/participant"):
                return self.page(path[1:] + ".html" if who(self.headers) else "gate.html")
            if path in ("/registration", "/join"):
                return self.page(path[1:] + ".html")
            if re.match(r"^/judge/(\d+)$", path):
                return self.page("judge.html" if hosted() or authed else "gate.html")
            if path in ("/judge", "/judge/"):
                return self.page("judges.html" if hosted() or authed else "gate.html")
            if path in ("/admin", "/admin/", "/desk", "/desk/"):
                name = "desk.html" if path.rstrip("/") == "/desk" else "admin.html"
                return self.page(name if hosted() or authed else "gate.html")
            if path == "/board":
                return self.page("board.html" if viewer else "gate.html")
            if path in ("/live", "/live/"):
                return self.page("live.html" if viewer else "gate.html")
            if path == '/media/registration-loop-v2.mp4':
                return self.registration_video()
            if path.startswith("/static/"):     # only the shared style and helper script; pages are served through their routes
                n = os.path.basename(path)
                assets = ("event-operations.js", "event-operations.css", "judging-operations.css", "live-transition.js", "live-transition.css", "agenda.js", "agenda.css", "accounts.js", "roles.css", "role-home.css", "app.css", "common.js", "event.css", "profile.css", "assistant.css", "assistant.js",
                          "participant-workspace.css", "participant-workspace.js", "participant-messages.js", "participant-desk.js",
                          "participant-prep.js", "participant-prep-player.js", "participant-push.js", "participant-icon.png", "event-quiz.js", "event-quiz.css", "audience-questions.js", "audience-questions.css",
                          "registration.css", "registration.js", "registration-join.js",
                          "live-hall.css", "live-player.js")
                return self.page(n) if n in assets else self.send(404, {"error": "not found"})
            if path == "/api/stations":     # the judge page lists stations and who is on them
                if not authed and not hosted():
                    return self.send(401, {"error": "password needed"})
                if hosted():
                    return self.send(200, {"stations": [{"station": st} for st in range(1, CFG["stations"] + 1)]})
                out = []
                for st in range(1, CFG["stations"] + 1):
                    lv = LIVE.get(st, {}); seen = time.time() - lv["t"] if lv.get("t") else None
                    out.append({"station": st, "judge": lv.get("judge", "") if seen is not None and seen < 30 else "", "online": seen is not None and seen < 10})
                return self.send(200, {"stations": out, "event": CONTENT["event"]["name"], "mode": mode()})
            if path in ("/api/public", "/api/board", "/api/info"):
                if not viewer and not (path == "/api/info" and who(self.headers)):
                    return self.send(401, {"error": "password needed"})
                if path == "/api/public":      # the slide page can also fetch it from another origin
                    return self.send(200, public_state(names=viewer == "named", presenter=q.get("presenter") == ["1"]), "application/json", {"Access-Control-Allow-Origin": "*"})
                if path == "/api/board":
                    return self.send(200, board_state(names=viewer == "named"))
                ip, host = ("", "") if hosted() or not authed else lan_info()
                ev = CONTENT["event"]
                info = {"stations": CFG["stations"], "rehearsal": CFG.get("rehearsal", False), "event": ev["name"], "headline": ev.get("headline", ""), "date": ev.get("date", ""),
                        "time": ev.get("time", ""), "venue": ev.get("venue", ""), "ip": ip, "host": host, "port": CFG["port"], "version": version_info(), "mode": mode(),
                        "ai": gateway_ready(), "ai_enabled": gateway_enabled(), "authed": authed}
                if WORKSPACE:
                    info["workspace"] = {"mode": WORKSPACE.mode, "context": WORKSPACE.context}
                if authed and not viewer == "anonymous":
                    info["view_key"] = anonymous_view_key() if hosted() else named_view_key()
                return self.send(200, info)
            me = who(self.headers)
            if not me:
                return self.send(401, {"error": "sign in"})
            if path == "/api/content":
                if me[0] not in ("admin", "desk", "judge"):
                    return self.send(403, {"error": "Staff or judge access is required."})
                c = dict(CONTENT)
                if (event_access.session(sys.modules[__name__],self.headers) or {}).get('dashboard')=='organizer':
                    c={k:c[k] for k in ('event','agenda','content_version') if k in c}
                if me[0] == "judge":
                    c["_station"] = me[1]
                return self.send(200, c)
            if path == "/api/state" and me[0] == "judge":
                try:
                    recover = int(q["final_pair"][0]) if q.get("final_pair") else None
                    return self.send(200, judge_state(me[1],recover))
                except ValueError as error:
                    return self.send(400,{"error":str(error)})
            if me[0] not in ("admin", "desk"):
                return self.send(403, {"error": "Staff access is required."})
            if hosted() and me[0] == "desk" and path not in event_access.DESK_GET:
                return self.send(403, {"error": "Admin access is required."})
            if path == "/api/desk/state":
                return self.send(200, event_roles.desk_projection(sys.modules[__name__],event_access.session(sys.modules[__name__],self.headers),desk_state()))
            if path == "/api/desk/scores":
                fb, f = final_board()
                return self.send(200, {"leaderboard": leaderboard(), "flags": flags(), "final": fb, "final_state": f, "reveal": kv_get("reveal", "hidden")})
            if path == "/api/desk/sheets":
                return self.send(200, {"sheets": active_sheets("round") + active_sheets("final")})
            if path == "/api/desk/calib":
                key = CONTENT["mock"]["key"]; ids = [t["id"] for g in CONTENT["rubric"] for t in g["ticks"]]
                rows = []
                for s in active_sheets("calib"):
                    diff = [ids[i] for i, t in enumerate(s["ticks"]) if bool(t) != bool(key[ids[i]]["earned"])]
                    rows.append({"station": s["station"], "judge": s["judge"], "ticks": s["ticks"], "diff": diff, "off": len(diff)})
                return self.send(200, {"rows": rows, "key": [1 if key[i]["earned"] else 0 for i in ids], "ids": ids})
            if path == "/api/desk/content":
                v = (q.get("v") or [""])[0]
                if v:
                    try:
                        return self.send(200, {"content": content_version(int(v))})
                    except (FileNotFoundError, ValueError):
                        return self.send(404, {"error": "no such version"})
                return self.send(200, {"content": {k: x for k, x in CONTENT.items() if not k.startswith("_")}, "versions": version_info(), "history": history_list(),
                                       "bundled": {k: x for k, x in bundled_content().items()}})
            if path == "/api/desk/content.json":
                return self.send(200, json.dumps({k: x for k, x in CONTENT.items() if not k.startswith("_")}, ensure_ascii=False, indent=1), "application/json",
                                 {"Content-Disposition": "attachment; filename=kevenos-content-v%s.json" % CONTENT.get("content_version", 1)})
            if path == "/api/desk/roster":
                with LOCK:
                    rows = event_config.list_roster(sys.modules[__name__])
                return self.send(200, {"roster": rows})
            if path == "/api/desk/invites" and hosted():
                return self.send(200, {"stations": judge_access()})
            if path == "/api/desk/links":
                if hosted():
                    return self.send(200, {"hosted": True, "stations": CFG["stations"]})
                ip, host = lan_info()
                return self.send(200, {"hosted": False, "stations": CFG["stations"], "ip": ip, "host": host, "port": CFG["port"], "desk_pin": CFG["desk_pin"], "password": CFG["password"], "view_key": named_view_key()})
            if path == "/api/desk/cards":
                return self.send(200, cards_html(), "text/html")
            if path == "/api/desk/export.csv":
                lb = leaderboard(); out = io.StringIO(); w = csv.writer(out)
                size=event_config.config(sys.modules[__name__])["team_size"]
                w.writerow(["rank", "pair", "partner", *["person%d"%i for i in range(1,size+1)], "round", "station", "judge", "discover", "mac_at_work", "solve", "close", "ticks", "overall_impression_1_to_5", "ai_tick", "badge", "total", "tie_break_needed", "content_version"])
                for r in lb:
                    names={m["id"]:m["name"] for m in r["members"]}
                    row = [r["rank"], r["pair"], r["partner"], *[names.get(i,"") for i in range(1,size+1)], r["round"], r["station"], r["judge"], *r["groups"], r["ticks"], r.get("wrap_rating",0), r["ai"], r["badge"], r["total"], "yes" if r["tie"] else "", r.get("content_version", "")]
                    w.writerow(map(csv_cell, row))
                return self.send(200, out.getvalue(), "text/csv", {"Content-Disposition": "attachment; filename=kevenos-scores.csv"})
            if path == "/api/desk/backup.json":
                with LOCK:
                    dump = {k: [dict(r) for r in DB.execute("SELECT * FROM " + k)] for k in ("sheets", "roster", "roster_members", "team_assignments", "audit", "kv")}
                return self.send(200, dump, "application/json", {"Content-Disposition": "attachment; filename=kevenos-backup.json"})
            return self.send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            print("Request failed: " + type(e).__name__, file=sys.stderr)
            return self.send(500, {"error": "Internal server error."})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.startup_response():
            return
        try:
            if WORKSPACE:
                WORKSPACE.sync()
            if WORKSPACE and WORKSPACE.guard.blocked:
                return self.send(503, {'error':'Event recovery is required. Contact the event administrator.'})
            return self._guarded_POST()
        except RuntimeError:
            if WORKSPACE and WORKSPACE.guard.blocked:
                return self.send(503, {'error':'Event recovery is required. Contact the event administrator.'})
            raise

    def _guarded_POST(self):
        if self.redirect_legacy_host():
            return
        if agent_api.handle(sys.modules[__name__], self) or agent_api.proxy_mcp(sys.modules[__name__], self):
            return
        # Read the bounded body before holding the workspace guard: slow clients
        # cannot keep a mode switch waiting while uploading a request.
        try:
            b = self.body()
        except (ValueError, UnicodeError):
            return self.send(400, {"error": "Invalid request body."})
        path = urlparse(self.path).path
        guard = (WORKSPACE.write() if path in ("/api/admin/live-transition","/api/admin/workspace","/api/admin/configuration","/api/assistant/confirm", "/api/admin/agenda") else WORKSPACE.read()) if WORKSPACE else nullcontext()
        with guard:
            refresh_content()
            if not event_access.same_origin(self):
                return self.send(403, {'error': 'Use this site to submit changes.'})
            if hosted() and 'application/json' not in self.headers.get('Content-Type', ''):
                return self.send(415, {'error': 'Use JSON for this request.'})
            identity_only = path in ("/api/access/login", "/api/access/logout", "/api/site-login", "/api/site-logout", "/api/role-logout", "/api/login", registration_desk.REDEEM)
            if WORKSPACE and not identity_only and self.headers.get("X-Event-Context") != WORKSPACE.context:
                return self.send(409, {"code": "workspace_changed", "error": "Event data changed. Reload this page before submitting changes."})
            try:
                # QR redemption establishes an identity, like password sign-in.
                # It checks the root session itself and never uses profile headers.
                if path != registration_desk.REDEEM and event_profiles.guard(sys.modules[__name__], self, path, write=True):
                    return
                serialized = path.startswith("/api/desk/") and path != "/api/desk/ai-summary"
                with LOCK if serialized else nullcontext():
                    self._dispatch_POST(b)
            finally:
                REQUEST_AUTH.identity = None

    def _dispatch_POST(self, b):
        try:
            if self.redirect_legacy_host():
                return
            u = urlparse(self.path); path = u.path
            secure = "; Secure" if hosted() else ""
            if not event_access.same_origin(self):
                return self.send(403, {"error": "Use this site to submit changes."})
            if hosted() and "application/json" not in self.headers.get("Content-Type", ""):
                return self.send(415, {"error": "Use JSON for this request."})
            if hosted() and path in ('/api/site-login', '/api/site-logout', '/api/login', '/api/role-logout'):
                return self.send(410, {'code': 'unified_signin', 'error': 'Use the role and password sign-in page.', 'next': '/'})
            if event_roles.guard(sys.modules[__name__],self,path,b):
                return
            if event_roles.handle(sys.modules[__name__],self,path,b):
                return
            if admin_agenda.handle(sys.modules[__name__], self, path, b):
                return
            if event_operations.handle(sys.modules[__name__], self, path, b):
                return
            if event_assistant.handle(sys.modules[__name__], self, path, b):
                return
            if event_profiles.handle_post(sys.modules[__name__], self, path, b):
                return
            ip_addr, _ = event_access.client_location(sys.modules[__name__], self)
            if event_config.handle_post(sys.modules[__name__], self, path, b):
                return
            if WORKSPACE and event_workspace.handle_post(sys.modules[__name__], self, path, b):
                return
            if event_mail.handle_post(sys.modules[__name__], self, path, b):
                return
            if event_quiz.handle(sys.modules[__name__], self, path, b):
                return
            if audience_questions.handle(sys.modules[__name__], self, path, b):
                return
            if event_push.handle(sys.modules[__name__], self, path, b):
                return
            if participant_workspace.handle(sys.modules[__name__], self, path, b):
                return
            if registration_desk.handle(sys.modules[__name__], self, path, b):
                return
            if event_access.handle_post(sys.modules[__name__], self, path, b):
                return
            if path == "/api/site-login":
                if throttled(ip_addr):
                    return self.send(429, {"error": "Too many tries. Wait a minute."})
                if match_secret(str(b.get("password", "")).strip().lower(), CFG["password"].lower()):
                    audit("site", "login", ip_addr)
                    return self.send(200, {"ok": True}, "application/json", {"Set-Cookie": "pp_site=%s; Path=/; Max-Age=172800; HttpOnly; SameSite=Lax%s" % (site_token(), secure)})
                throttled(ip_addr, True)
                return self.send(403, {"error": "That password is not right."})
            if path == "/api/site-logout":
                return self.send(200, {"ok": True}, "application/json", {"Set-Cookie": "pp_site=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + secure})
            if path == "/api/login":
                if hosted() and b.get("role") == "judge":
                    station = b.get("station")
                    if type(station) is not int or not 1 <= station <= CFG["stations"]:
                        return self.send(400, {"error": "Choose the station assigned to you."})
                    session = redeem_invite(station, b.get("invite"))
                    if not session:
                        return self.send(403, {"error": "This invitation is invalid, expired, or already used. Ask the organiser for a new one."})
                    self._activity_identity = {"role": "judge", "station": station, "label": session["judge"], "credential_id": "judge-station-" + str(station)}
                    audit("judge%d" % station, "invite-redeemed")
                    return self.send(200, session)
                if not site_ok(self.headers):
                    return self.send(401, {"error": "Enter the event password first."})
                if throttled(ip_addr):
                    return self.send(429, {"error": "Too many tries. Wait a minute."})
                role = b.get("role")
                if role == "desk" and match_secret(b.get("pin", ""), CFG["desk_pin"]):
                    if hosted():
                        with LOCK, DB:
                            token = new_role_session("admin")
                    else:
                        token = token_for("desk", 0)
                    self._activity_identity = {"role": "admin" if hosted() else "desk", "credential_id": "owner-recovery", "label": "Owner"}
                    audit("desk", "login")
                    return self.send(200, {"token": token, "role": "admin" if hosted() else "desk"})
                if role == "desk" and match_secret(b.get("pin", ""), CFG["password"]):
                    return self.send(403, {"error": "Enter the organiser PIN for the Admin Panel."})
                if role == "judge":
                    s = int(b.get("station", 0))
                    if 1 <= s <= CFG["stations"] and str(b.get("name", "")).strip():
                        audit("judge%d" % s, "login", b.get("name", "")); return self.send(200, {"token": token_for("judge", s), "role": "judge", "station": s})
                    return self.send(400, {"error": "Please enter your name."})
                throttled(ip_addr, True)
                return self.send(403, {"error": "That PIN is not right."})
            me = who(self.headers)
            if not me:
                return self.send(401, {"error": "sign in"})
            role, station, judge = me
            if path == "/api/role-logout" and hosted():
                self._activity_identity = event_access.session(sys.modules[__name__], self.headers)
                auth = event_access.auth_db(sys.modules[__name__])
                with LOCK, auth:
                    auth.execute("DELETE FROM role_sessions WHERE digest=?", (credential_digest(self.headers.get("X-Token") or cookie_of(self.headers, "pp_access")),))
                return self.send(200, {"ok": True})
            if role == "judge":
                if judge:
                    b["judge"] = judge
                if path == "/api/sync":
                    LIVE[station] = {"t": time.time(), "ticks": b.get("ticks"), "asked": b.get("asked"), "judge": str(b.get("judge", ""))[:60]}
                    try:
                        return self.send(200, judge_state(station,b.get("final_pair")))
                    except ValueError as error:
                        return self.send(400,{"error":str(error)})
                if path == "/api/submit":
                    with LOCK:
                        if hosted() and event_profiles.guard(sys.modules[__name__],self,path,write=True):return
                        if hosted() and who(self.headers)!=me:return self.send(401,{"error":"Your judging assignment changed."})
                        res, code = save_sheet(station, b, "judge%d" % station)
                    return self.send(code, res)
                if path == "/api/session":
                    with LOCK:
                        if hosted() and event_profiles.guard(sys.modules[__name__],self,path,write=True):return
                        if hosted() and who(self.headers)!=me:return self.send(401,{"error":"Your judging assignment changed."})
                        res, code = judge_session(station, b)
                    return self.send(code, res)
                if path == "/api/ai/feedback":
                    try:
                        current_state=judge_state(station,b.get("final_pair"))
                    except ValueError as error:
                        return self.send(400,{"error":str(error)})
                    d = {"ticks": [1 if x else 0 for x in b.get("ticks", [])][:10], "notes": b.get("notes", ""), "asked": b.get("asked", []), "scenario": current_state["scenario"], "scenario_snapshot":current_state.get("scenario_snapshot")}
                    d["ticks"] += [0] * (10 - len(d["ticks"]))
                    tpl = template_feedback(d["ticks"])
                    try:
                        return self.send(200, {"text": ai_feedback(d), "source": "ai", "template": tpl})
                    except Exception:  # noqa: BLE001
                        return self.send(200, {"text": tpl, "source": "template", "template": tpl, "why": AI_FALLBACK})
                if path == "/api/ai/tidy":
                    try:
                        return self.send(200, {"text": ai_tidy(b.get("notes", "")), "source": "ai"})
                    except Exception:  # noqa: BLE001
                        return self.send(200, {"text": b.get("notes", ""), "source": "unchanged", "why": AI_FALLBACK})
                return self.send(403, {"error": "judge cannot do that"})
            if path.startswith("/api/desk/"):
                operation = b.get("action") or b.get("mode")
                if operation in ("start", "pause", "reset", "next", "prev", "set", "stop", "goto", "mode", "announce", "scene", "live", "off", "hidden", "top3", "all", "self", "desk", "void", "ticks", "bundled", "issue", "revoke"):
                    self._activity_action = path.rsplit("/", 1)[-1] + "-" + operation
            # ---- staff: day operations and administration are separate authorities ----
            if role not in ("admin", "desk"):
                return self.send(403, {"error": "Staff access is required."})
            if hosted() and role == "desk" and path not in event_access.DESK_POST:
                return self.send(403, {"error": "Admin access is required."})
            if path == "/api/desk/invites" and hosted():
                return self.send(410, {'error': 'Create or reset a Staff password in Admin. Judge invitations are no longer issued.'})
            if path == "/api/desk/mode":
                result, code = judging_mode_command(b, "desk")
                return self.send(code, result)
            if path == "/api/desk/clock":
                final = final_event.get(sys.modules[__name__])
                if final_event.engaged(final):
                    if b.get("expected_round") is not None or b.get("review_token") is not None:
                        return self.send(409, {"error": "The Final is on stage. Refresh before using its clock."})
                    result, code = final_clock_action(final, b.get("action"), b)
                else:
                    result, code = preliminary_clock_command(b, "desk")
                    if code == 200:
                        result = clock_public(b.get("station"))
                return self.send(code, result)
            if path == "/api/desk/content":
                new = bundled_content() if b.get("action") == "bundled" else b.get("content")
                errs = validate_content(new)
                if errs:
                    return self.send(400, {"error": "Not saved: " + " ".join(errs[:4]), "problems": errs})
                expected = b.get('expected_content_version', new.get('content_version') if b.get('action') != 'bundled' else None)
                if type(expected) is not int or expected != CONTENT.get('content_version', 1):
                    return self.send(409, {'code': 'content_changed', 'error': 'The event content changed since this editor was loaded. Your draft has not been saved. Reload the latest version before saving.', 'current_version': CONTENT.get('content_version', 1)})
                v = save_content(new, b.get("note") or ("Reverted to the bundled text" if b.get("action") == "bundled" else ""))
                return self.send(200, {"ok": True, "version": v, "versions": version_info()})
            if path == "/api/desk/program":
                pr = program_now()
                action = b.get("action")
                if action in ("set", "next", "prev"):
                    index = b.get("index", pr["i"]) if action == "set" else pr["i"] + (1 if action == "next" else -1)
                    if type(index) is not int:
                        return self.send(400, {"error": "Choose a valid programme step."})
                    select_program(max(0, min(len(agenda()) - 1, index)))
                elif action == "mode" and b.get("mode") in ("manual", "clock"):
                    kv_set("program", dict(pr, mode=b["mode"]))
                elif action == "announce":
                    channel = b.get("channel", "both")
                    if type(b.get("important", False)) is not bool or channel not in ("room", "phones", "both"):
                        return self.send(400, {"error": "Choose where to show the announcement."})
                    program_announcement(str(b.get("text", ""))[:200], channel, b.get("important", False))
                elif action == "return":
                    program_scene("auto")
                elif action == "scene" and (b.get("scene") in SCENES or b.get("scene") == "auto"):
                    program_scene(b["scene"])
                else:
                    return self.send(400, {"error": "Choose a programme or room action."})
                pr = program_now()
                audit("desk", "program-" + action, str(pr.get("i")) if action in ("set", "next", "prev") else "")
                return self.send(200, {"program": pr})
            if path == "/api/desk/public":
                m = b.get("mode")
                if m not in ("live", "off"):
                    return self.send(400, {"error": "mode must be live or off"})
                kv_set("public", m); audit("desk", "public", m); return self.send(200, {"mode": m})
            if path == "/api/desk/reveal":
                reveal = b.get("mode","hidden")
                if reveal not in ("hidden","top3","winners"):
                    return self.send(400,{"error":"Choose a valid audience reveal."})
                with LOCK:
                    if reveal == "winners":
                        f=final_event.get(sys.modules[__name__])
                        if f.get("status")=="completed":
                            kv_set("reveal","winners"); result,code={"ok":True},200
                        else:
                            result,code=final_action({"action":"publish","confirm":"PUBLISH"})
                    else:
                        kv_set("reveal",reveal); audit("desk","reveal",reveal); result,code={"ok":True},200
                return self.send(code,result)
            if path == "/api/desk/final":
                with LOCK:
                    result, code = final_action(b)
                return self.send(code, result)
            if path == "/api/desk/calib":
                if final_event.engaged(final_event.get(sys.modules[__name__])):
                    return self.send(409,{"error":"The Final is in progress or completed; calibration cannot replace it."})
                kv_set("calib", {"active": bool(b.get("active"))}); audit("desk", "calib", b.get("active")); return self.send(200, {"ok": True})
            if path == "/api/desk/sheet":
                if (event_access.session(sys.modules[__name__],self.headers) or {}).get("dashboard")=="scoring" and (not isinstance(b.get("why"),str) or not 3<=len(b["why"].strip())<=300):
                    return self.send(400,{"error":"Enter a short reason for the score correction."})
                sid = int(b.get("id", 0)); act = b.get("action")
                with LOCK:
                    sheet = DB.execute("SELECT kind,final_run,wrap_rating,final_rating FROM sheets WHERE id=?",(sid,)).fetchone()
                    f=final_event.get(sys.modules[__name__])
                    if sheet and sheet["kind"]=="final" and sheet["final_run"]==f.get("run_id") and f.get("status")=="completed":
                        return self.send(409,{"error":"Published Final scores are locked. Reopen results with a reason first."})
                    if sheet and sheet["kind"]=="final":final_event.invalidate_decision(sys.modules[__name__],f)
                    if act == "void":
                        DB.execute("UPDATE sheets SET voided=1, edited_by='desk' WHERE id=?", (sid,))
                    elif act == "ticks":
                        t = [1 if x else 0 for x in b.get("ticks", [])][:10]; t += [0] * (10 - len(t))
                        wrap_rating=b.get('wrap_rating',sheet['wrap_rating']);final_rating=b.get('final_rating',sheet['final_rating'])
                        required=final_rating if sheet['kind']=='final' else wrap_rating if sheet['kind']=='round' else 1
                        if type(required) is not int or not 1<=required<=5:
                            return self.send(400,{"error":"Choose the applicable rating from one to five."})
                        DB.execute("UPDATE sheets SET ticks=?, ai=?, wrap_rating=?, final_rating=?, edited_by='desk' WHERE id=?", (json.dumps(t), 1 if b.get("ai") else 0,wrap_rating,final_rating,sid))
                    DB.commit()
                audit("desk", "sheet-" + str(act), "sheet %d %s" % (sid, b.get("why", "")))
                return self.send(200, {"ok": True})
            if path == "/api/desk/roster":
                try:
                    rows=b.get("rows")
                    if not isinstance(rows,list):
                        if not isinstance(b.get("csv",""),str):
                            raise ValueError("Provide roster CSV text or a list of team rows.")
                        rows=parse_roster(b.get("csv",""))
                    count=event_config.save_roster(sys.modules[__name__],rows)
                except ValueError as error:
                    return self.send(400,{"error":str(error)})
                audit("desk","roster","%d teams"%count)
                return self.send(200,{"ok":True,"rows":count})
            if path == "/api/desk/ai-summary":
                try:
                    text, rates = ai_summary()
                    kv_set("summary", text)
                    return self.send(200, {"text": text, "rates": rates, "source": "ai"})
                except Exception:  # noqa: BLE001
                    rates = rubric_rates()
                    rates_sorted = sorted(rates, key=lambda r: r[1] / max(1, r[2]))
                    text = "Most missed: " + "; ".join("%s (%d of %d pairs)" % r for r in rates_sorted[:3]) + ".\nMost common: " + "; ".join("%s (%d of %d)" % r for r in rates_sorted[::-1][:3]) + "."
                    return self.send(200, {"text": text, "rates": rates, "source": "template", "why": AI_FALLBACK})
            if path == "/api/desk/reset":
                if WORKSPACE and WORKSPACE.mode == "rehearsal":
                    return self.send(409, {"error": "Use Admin → System → Reset rehearsal data so open pages and queued scores are refreshed safely."})
                if b.get("confirm") != "RESET":
                    return self.send(400, {"error": "type RESET"})
                with LOCK:
                    for t in ("sheets", "audit"):
                        DB.execute("DELETE FROM " + t)
                    DB.execute("DELETE FROM kv WHERE k NOT IN ('workspace_control', 'workspace_seeded', 'schema_version', 'event_configuration')")
                    DB.commit()
                LIVE.clear(); audit("desk", "reset"); return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            print("Request failed: " + type(e).__name__, file=sys.stderr)
            return self.send(500, {"error": "Internal server error."})


def main():
    global CONTENT, CFG, DB, WORKSPACE, STARTING
    STARTING = True
    READY.clear()
    startup_started = time.monotonic()
    def startup_trace(stage):
        if hosted():
            print('Startup %s %.2fs' % (stage, time.monotonic() - startup_started), file=sys.stderr, flush=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from the judge solution devices; 127.0.0.1 = this solution device only")
    ap.add_argument("--db", default=os.path.join(DATA, "judging.db"))
    ap.add_argument("--config", default=os.path.join(DATA, "config.json") if os.environ.get("KEVENOS_DATA") else os.path.join(HERE, "config.json"))
    ap.add_argument("--mode", choices=("self", "desk"), help="self = judges start and end their own sessions (default); desk = one shared clock")
    ap.add_argument("--rehearsal", action="store_true", help="mark this run as a rehearsal (shown on every screen)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    CFG = load_config(a.config)
    for env, key in (("KEVENOS_PASSWORD", "password"), ("KEVENOS_DESK_PIN", "desk_pin"),
                     ("KEVENOS_PUBLIC_SCORES", "public"), ("KEVENOS_VIEW_KEY", "view_key")):
        if (not hosted() or env == "KEVENOS_PUBLIC_SCORES") and os.environ.get(env):
            CFG[key] = os.environ[env]
    if os.environ.get("KEVENOS_PORT"):
        CFG["port"] = int(os.environ["KEVENOS_PORT"])
    if a.port:
        CFG["port"] = a.port
    if a.rehearsal:
        CFG["rehearsal"] = True
    if a.mode:
        CFG["mode"] = a.mode
    startup_trace('configuration')
    DB = open_db(a.db)
    startup_trace('live-database')
    CONTENT = load_content()
    srv = EventHTTPServer((a.host, CFG["port"]), Handler)
    srv.daemon_threads = True
    server_thread = threading.Thread(target=srv.serve_forever, name='kevenos-http', daemon=True)
    server_thread.start()
    startup_trace('listening')
    WORKSPACE = event_workspace.initialize(sys.modules[__name__], a.db, DATA)
    startup_trace('workspace')
    if a.rehearsal:
        WORKSPACE.switch("rehearsal", "REHEARSAL")
    startup_trace('workspace-selection')
    event_access.seed_guest(sys.modules[__name__])
    event_access.seed_demo_roles(sys.modules[__name__])
    startup_trace('demo-access')
    event_assistant.recover(sys.modules[__name__])
    event_push.start(sys.modules[__name__])
    startup_trace('services')
    ip, host = ("", "") if hosted() else lan_info()
    if a.mode:
        kv_set("mode", a.mode)
    if os.environ.get("KEVENOS_AGENT_PRINCIPALS_FILE"):
        import agent_mcp
        threading.Thread(target=agent_mcp.start_remote, kwargs={"api_base":"http://127.0.0.1:%d" % CFG["port"], "port":int(os.environ.get("KEVENOS_MCP_PORT", "8081"))}, daemon=True, name="kevenos-mcp").start()
    print("KevenOS Admin v%s%s" % (APP_VERSION, " (REHEARSAL)" if CFG.get("rehearsal") else ""))
    print("  Content   v%s (%s%s)" % (CONTENT.get("content_version", 1), CONTENT["_source"], ": " + content_path() if CONTENT["_source"] == "edited" else ""))
    if hosted():
        print("  Address   %s" % os.environ.get("KEVENOS_PUBLIC_URL", "configured by deployment"))
    else:
        print("  Address   http://%s:%d" % (host or ip or "localhost", CFG["port"]))
        print("  Local access codes are in the configuration file.")
    print("  Mode      %s" % ("judges start and end their own sessions" if mode() == "self" else "one shared clock run by the desk"))
    print("  AI        %s" % ("OpenAI-compatible AI service (%s)" % GATEWAY_ROLE if gateway_enabled() else "Templates"))
    print("  Database  %s" % database.resolve_backend().name)
    STARTING = False
    READY.set()
    try:
        server_thread.join()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()
        server_thread.join()


if __name__ == "__main__":
    main()
