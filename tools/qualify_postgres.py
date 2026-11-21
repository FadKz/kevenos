#!/usr/bin/env python3
"""Qualify two KevenOS processes against one PostgreSQL/Supabase database."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("admin", "organizer", "scoring", "registration", "judge", "participant", "guest")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def request(port: int, method: str, path: str, body=None, cookie: str = "", extra_headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    headers.update(extra_headers or {})
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read()
    result = json.loads(data) if data else {}
    cookies = [value.split(";", 1)[0] for name, value in response.getheaders() if name.lower() == "set-cookie"]
    context = response.getheader("X-Event-Context")
    connection.close()
    return response.status, result, cookies, context


def wait_ready(port: int) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status, _, _, context = request(port, "GET", "/api/access/me")
            if status == 200 and context:
                return context
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"KevenOS process on port {port} did not become healthy.")


def real_columns(database_url: str, schemas) -> list:
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as raw:
        return raw.execute(
            "SELECT table_schema, table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = ANY(%s) AND data_type='real' ORDER BY 1,2,3", (list(schemas),)).fetchall()


def qualify_storage(database_url: str, prefix: str) -> None:
    """Check the connection layer against a real server before any process starts."""
    import psycopg

    sys.path.insert(0, str(ROOT))
    import database

    # A schema of its own with a random name, so nothing that already exists is touched.
    config = database.BackendConfig(name="postgres", url=database_url, schema_prefix=f"{prefix}_q{uuid.uuid4().hex[:8]}")
    schema = database.schema_for_path("judging.db", config.schema_prefix)
    connection = None
    try:
        with psycopg.connect(database_url, autocommit=True) as raw:
            raw.execute('CREATE SCHEMA "%s"' % schema)
            # A table as releases up to 1.2.0 created it: a 4-byte timestamp column.
            raw.execute('CREATE TABLE "%s".legacy_audit (id BIGSERIAL PRIMARY KEY, ts REAL NOT NULL, note TEXT)' % schema)
            raw.execute('INSERT INTO "%s".legacy_audit(ts,note) VALUES (1795257771.173, %%s)' % schema, ("kept",))
        connection = database.connect("judging.db", config)
        # A table that requests are using is postponed, not waited for and not fatal.
        with psycopg.connect(database_url) as busy:
            busy.execute('LOCK TABLE "%s".legacy_audit IN ACCESS SHARE MODE' % schema)
            started = time.monotonic()
            postponed = database.widen_real_columns(connection, lock_timeout_ms=300)
            waited = time.monotonic() - started
            busy.rollback()
        if postponed != 0 or len(real_columns(database_url, [schema])) != 1 or waited > 5:
            raise RuntimeError(f"A busy table was not postponed cleanly: converted {postponed} after {waited:.1f} s.")
        converted = database.widen_real_columns(connection)
        repeated = database.widen_real_columns(connection)
        if converted != 1 or repeated != 0 or real_columns(database_url, [schema]):
            raise RuntimeError(f"Legacy timestamp columns were not widened exactly once: {converted}, {repeated}.")
        if connection.execute("SELECT note FROM legacy_audit").fetchone()[0] != "kept":
            raise RuntimeError("Widening a timestamp column lost existing rows.")
        connection.execute("CREATE TABLE IF NOT EXISTS stamps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL)")
        now = time.time()
        connection.execute("INSERT INTO stamps(ts) VALUES(?)", (now,))
        stored = connection.execute("SELECT ts FROM stamps").fetchone()[0]
        if abs(stored - now) > 0.001:
            raise RuntimeError(f"A stored timestamp differs from the written value by {stored - now:.1f} s.")
        with connection:
            database.begin_immediate(connection)
            connection.execute("INSERT INTO stamps(ts) VALUES(?)", (now,))
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(connection.execute("SELECT count(*) FROM stamps").fetchone()[0]), daemon=True)
        worker.start()
        worker.join(timeout=10)
        if outcome != [2]:
            raise RuntimeError("The connection stayed locked after a transaction block that began a transaction.")
    finally:
        if connection is not None:
            connection.close()
        with psycopg.connect(database_url, autocommit=True) as raw:
            raw.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)


def legacy_probe(database_url: str, schema: str, create: bool) -> None:
    """Plant (or remove) a table shaped like one from release 1.2.0 so start-up conversion is exercised."""
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as raw:
        if create:
            raw.execute('CREATE SCHEMA IF NOT EXISTS "%s"' % schema)
            raw.execute('CREATE TABLE IF NOT EXISTS "%s".qualification_legacy_probe (id BIGSERIAL PRIMARY KEY, ts REAL)' % schema)
        else:
            raw.execute('DROP TABLE IF EXISTS "%s".qualification_legacy_probe' % schema)


def main() -> int:
    database_url = os.environ.get("KEVENOS_TEST_DATABASE_URL", "")
    if not database_url:
        print("KEVENOS_TEST_DATABASE_URL is required.", file=sys.stderr)
        return 2
    prefix = os.environ.get("KEVENOS_TEST_SCHEMA_PREFIX", "kevenos_ci")
    qualify_storage(database_url, prefix)
    legacy_probe(database_url, prefix + "_live", create=True)
    ports = (free_port(), free_port())
    processes = []
    with tempfile.TemporaryDirectory(prefix="kevenos-postgres-qualification-") as folder:
        for index, port in enumerate(ports):
            environment = os.environ.copy()
            environment.update({
                "KEVENOS_DB_BACKEND": os.environ.get("KEVENOS_TEST_DB_BACKEND", "postgres"),
                "KEVENOS_DATABASE_URL": database_url,
                "KEVENOS_DB_SCHEMA_PREFIX": prefix,
                "KEVENOS_PASSWORD": "admin",
                "KEVENOS_GUEST_PASSWORD": "guest",
                "KEVENOS_VIEW_KEY": "qualification",
                "KEVENOS_SESSION_SECRET": "synthetic-qualification-session-secret",
                "KEVENOS_DEMO_SCRATCH": str(Path(folder) / str(index)),
                "PORT": str(port),
                "VERCEL_URL": f"127.0.0.1:{port}",
            })
            processes.append(subprocess.Popen(
                [sys.executable, str(ROOT / "tools" / "vercel_entrypoint.py")],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ))
        try:
            contexts = tuple(wait_ready(port) for port in ports)
            if contexts[0] != contexts[1]:
                raise RuntimeError("Independent processes returned different event contexts.")
            for role in ROLES:
                status, identity, cookies, _ = request(ports[0], "POST", "/api/access/login", {"password": role})
                if status != 200 or identity.get("role") != role:
                    raise RuntimeError(f"{role} demo sign-in failed: HTTP {status} {identity!r}")
                status, shared, _, _ = request(ports[1], "GET", "/api/access/me", cookie="; ".join(cookies))
                dashboard = shared.get("dashboard", shared.get("role"))
                if status != 200 or not shared.get("authenticated") or dashboard != role:
                    raise RuntimeError(f"{role} session was not shared across processes: {shared!r}")
            status, admin, cookies, event_context = request(ports[0], "POST", "/api/access/login", {"password": "admin"})
            cookie = "; ".join(cookies)
            headers = {"X-Event-Context": event_context, "X-Access-Context": admin["context"]}
            status, activity, _, _ = request(ports[0], "GET", "/api/admin/activity", cookie=cookie)
            if status != 200 or "summary" not in activity or not activity.get("events"):
                raise RuntimeError(f"Admin activity is unavailable on PostgreSQL: HTTP {status} {activity!r}")
            # Events are listed newest first; rows written by release 1.2.0 before an upgrade stay rounded.
            newest = activity["events"][0]["ts"]
            # A 4-byte column stores a whole multiple of 128 seconds, up to 64 s from the real time.
            if abs(time.time() - newest) > 30 or newest == int(newest):
                raise RuntimeError(f"Activity timestamps are imprecise: the newest is {time.time() - newest:.0f} s from now.")
            leftover = real_columns(database_url, [prefix + "_live", prefix + "_rehearsal"])
            if leftover:
                raise RuntimeError(f"Start-up left timestamp columns in 4-byte storage: {leftover[:5]!r}")
            status, editor, _, _ = request(ports[0], "GET", "/api/desk/content", cookie=cookie)
            if status != 200:
                raise RuntimeError(f"Could not read content from process A: {editor!r}")
            content = editor["content"]
            content["event"]["name"] = "Shared database qualification"
            body = {"content": content, "expected_content_version": editor["versions"]["content"], "note": "PostgreSQL qualification"}
            status, saved, _, _ = request(ports[0], "POST", "/api/desk/content", body, cookie, headers)
            if status != 200:
                raise RuntimeError(f"Could not save content through process A: {saved!r}")
            status, observed, _, _ = request(ports[1], "GET", "/api/desk/content", cookie=cookie)
            if status != 200 or observed["content"]["event"]["name"] != "Shared database qualification":
                raise RuntimeError(f"Content mutation was not visible on process B: {observed!r}")
            status, live, _, live_context = request(ports[0], "POST", "/api/admin/workspace",
                                                    {"mode": "live", "confirm": "LIVE"}, cookie, headers)
            if status != 200 or live.get("mode") != "live":
                raise RuntimeError(f"Could not switch process A to Live: {live!r}")
            status, _, _, observed_context = request(ports[1], "GET", "/api/access/me")
            if status != 200 or observed_context != live_context:
                raise RuntimeError("Process B did not adopt the shared Live workspace context.")
            headers["X-Event-Context"] = live_context
            status, rehearsal, _, rehearsal_context = request(ports[0], "POST", "/api/admin/workspace",
                                                               {"mode": "rehearsal", "confirm": "REHEARSAL"}, cookie, headers)
            if status != 200 or rehearsal.get("mode") != "rehearsal":
                raise RuntimeError(f"Could not return process A to Rehearsal: {rehearsal!r}")
            headers["X-Event-Context"] = rehearsal_context
            status, reset, _, reset_context = request(ports[0], "POST", "/api/admin/workspace",
                                                       {"action": "reset-rehearsal", "confirm": "RESET REHEARSAL"}, cookie, headers)
            if status != 200 or not reset.get("seeded"):
                raise RuntimeError(f"Shared Rehearsal reset failed: {reset!r}")
            status, _, _, observed_context = request(ports[1], "GET", "/api/access/me")
            if status != 200 or observed_context != reset_context:
                raise RuntimeError("Process B did not adopt the reset Rehearsal context.")
            print("PostgreSQL qualification passed: storage precision and locking, shared context, sessions, activity, "
                  "content, workspace changes, reset and demo roles.")
            return 0
        finally:
            legacy_probe(database_url, prefix + "_live", create=False)
            for process in processes:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            for process in processes:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is None:
                    process.kill()
                    process.wait()
                output = process.stdout.read() if process.stdout else ""
                if process.returncode and output:
                    print(output, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
