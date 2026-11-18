#!/usr/bin/env python3
"""Start the disposable KevenOS Vercel demo without persisting secrets."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import MutableMapping


SECRET_NAMES = (
    "KEVENOS_PASSWORD",
    "KEVENOS_GUEST_PASSWORD",
    "KEVENOS_VIEW_KEY",
    "KEVENOS_SESSION_SECRET",
)


def prepare(environment: MutableMapping[str, str], scratch: Path = Path("/tmp/kevenos")) -> list[str]:
    missing = [name for name in SECRET_NAMES if not environment.get(name)]
    if missing:
        raise RuntimeError("Missing required Vercel demo secret(s): " + ", ".join(missing))

    if environment.get('VERCEL_ENV') == 'preview':
        commit = environment.get('VERCEL_GIT_COMMIT_SHA', '')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
            raise RuntimeError('Preview requires a full VERCEL_GIT_COMMIT_SHA for database isolation.')
        environment['KEVENOS_DB_SCHEMA_PREFIX'] = 'kevenos_preview_' + commit[:24].lower()

    root = Path(__file__).resolve().parents[1]
    secret_dir = scratch / "secrets"
    data_dir = scratch / "data"
    secret_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    secret_dir.chmod(0o700)
    data_dir.chmod(0o700)

    for name in SECRET_NAMES:
        value = environment.pop(name)
        path = secret_dir / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)

    public_host = environment.get("VERCEL_URL") or environment.get("VERCEL_PROJECT_PRODUCTION_URL")
    environment.update(
        KEVENOS_HOSTED="1",
        KEVENOS_GATEWAY_ENABLED="0",
        KEVENOS_ASSISTANT_ENABLED="0",
        KEVENOS_SMTP_ENABLED="0",
        KEVENOS_PUSH_ENABLED="0",
        KEVENOS_DEMO_ROLE_PASSWORDS="1",
        KEVENOS_STABLE_DEMO_CONTEXT="1",
        KEVENOS_DATA=str(data_dir),
        KEVENOS_SECRETS_DIR=str(secret_dir),
        KEVENOS_PORT=environment.get("PORT", "8080"),
        KEVENOS_SERVER_SOURCE=str(root / "server.py"),
        TZ="UTC",
    )
    if public_host:
        environment["KEVENOS_PUBLIC_URL"] = "https://" + public_host.removeprefix("https://")

    return [sys.executable, str(root / "database_guard.py"), "--rehearsal"]


def main() -> None:
    scratch = Path(os.environ.pop("KEVENOS_DEMO_SCRATCH", "/tmp/kevenos"))
    command = prepare(os.environ, scratch)
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
