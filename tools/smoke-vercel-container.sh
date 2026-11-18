#!/usr/bin/env bash
set -euo pipefail

image="${1:-kevenos-vercel-demo:smoke}"
container="kevenos-vercel-smoke-$$"
port="${KEVENOS_SMOKE_PORT:-18085}"

cleanup() {
  docker stop "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm --name "$container" -d -p "127.0.0.1:${port}:8080" \
  -e PORT=8080 \
  -e VERCEL_URL=kevenos-demo.local \
  -e KEVENOS_PASSWORD=Demo-Owner-Password-2026 \
  -e KEVENOS_GUEST_PASSWORD=Demo-Guest-Password-2026 \
  -e KEVENOS_VIEW_KEY=demo-view-key-2026 \
  -e KEVENOS_SESSION_SECRET=demo-session-secret-2026 \
  "$image" >/dev/null

KEVENOS_SMOKE_ORIGIN="http://127.0.0.1:${port}" python3 - <<'PY'
import json
import os
import time
import urllib.request

base = os.environ["KEVENOS_SMOKE_ORIGIN"]
for _ in range(80):
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=1) as response:
            if json.load(response).get("ok") is True:
                break
    except Exception:
        time.sleep(0.25)
else:
    raise SystemExit("container health check timed out")

with urllib.request.urlopen(base + "/", timeout=2) as response:
    if "KevenOS Competition" not in response.read().decode():
        raise SystemExit("KevenOS branding is missing")

for password, expected_role in (
    ("Demo-Guest-Password-2026", "guest"),
    ("Demo-Owner-Password-2026", "admin"),
):
    request = urllib.request.Request(
        base + "/api/access/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
        cookie = response.headers["Set-Cookie"]
    if payload.get("role") != expected_role:
        raise SystemExit(f"expected {expected_role} login")
    if not all(flag in cookie for flag in ("Secure", "HttpOnly", "SameSite=Lax")):
        raise SystemExit("hosted session cookie is not secure")

print("Vercel container smoke passed")
PY
