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
import time


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
            status, _, _, context = request(port, "GET", "/healthz")
            if status == 200 and context:
                return context
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"KevenOS process on port {port} did not become healthy.")


def main() -> int:
    database_url = os.environ.get("KEVENOS_TEST_DATABASE_URL", "")
    if not database_url:
        print("KEVENOS_TEST_DATABASE_URL is required.", file=sys.stderr)
        return 2
    ports = (free_port(), free_port())
    processes = []
    with tempfile.TemporaryDirectory(prefix="kevenos-postgres-qualification-") as folder:
        for index, port in enumerate(ports):
            environment = os.environ.copy()
            environment.update({
                "KEVENOS_DB_BACKEND": os.environ.get("KEVENOS_TEST_DB_BACKEND", "postgres"),
                "KEVENOS_DATABASE_URL": database_url,
                "KEVENOS_DB_SCHEMA_PREFIX": os.environ.get("KEVENOS_TEST_SCHEMA_PREFIX", "kevenos_ci"),
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
            status, _, _, observed_context = request(ports[1], "GET", "/healthz")
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
            status, _, _, observed_context = request(ports[1], "GET", "/healthz")
            if status != 200 or observed_context != reset_context:
                raise RuntimeError("Process B did not adopt the reset Rehearsal context.")
            print("PostgreSQL qualification passed: shared context, sessions, content, workspace changes, reset and demo roles.")
            return 0
        finally:
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
