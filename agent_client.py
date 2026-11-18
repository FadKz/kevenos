"""Async client for the KevenOS agent API."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2

DEFAULT_URL = os.environ.get("KEVENOS_AGENT_API_URL", "http://127.0.0.1:8080")
MAX_RESPONSE = 2_000_000
MAX_TOKEN = 256
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{40,256}", re.ASCII)
ENDPOINTS = {
    "status": "GET",
    "capabilities": "GET",
    "openapi.json": "GET",
    "guide": "GET",
    "read": "POST",
    "prepare": "POST",
    "execute": "POST",
    "receipts": "POST",
}


class AgentError(Exception):
    """A transport or API error with a safe, structured envelope."""

    def __init__(self, status: int, envelope: dict[str, Any]):
        self.status = status
        self.envelope = envelope
        super().__init__(envelope["error"]["message"])


def failure(status: int, code: str, message: str, retryable: bool = False) -> AgentError:
    return AgentError(status, {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    })


def valid_token(token: str) -> bool:
    return len(token) <= MAX_TOKEN and TOKEN_PATTERN.fullmatch(token) is not None


def token_from_env() -> str:
    token = os.environ.get("KEVENOS_AGENT_TOKEN")
    token_file = os.environ.get("KEVENOS_AGENT_TOKEN_FILE")
    if token and token_file:
        raise ValueError("Configure only one KevenOS agent credential source.")
    if token_file:
        try:
            with Path(token_file).open("rb") as stream:
                token = stream.read(MAX_TOKEN + 2).decode("ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("The KevenOS agent credential file could not be read.") from None
    if not token or not valid_token(token):
        raise ValueError("Set KEVENOS_AGENT_TOKEN or KEVENOS_AGENT_TOKEN_FILE to a valid agent credential.")
    return token


def api_url(base: str) -> str:
    url = urlsplit(base)
    if (
        not url.hostname or url.username is not None or url.password is not None
        or url.path not in ("", "/") or url.query or url.fragment
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"})
        )
    ):
        raise ValueError("Use an HTTPS origin or an HTTP loopback origin for the agent API.")
    return base.rstrip("/") + "/api/v1/agent"


class AgentClient:
    def __init__(self, base: str = DEFAULT_URL, *, timeout: float = 30.0):
        self.url = api_url(base)
        self.timeout = timeout

    async def request(
        self, endpoint: str, token: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not valid_token(token):
            raise failure(401, "unauthorized", "A valid bearer credential is required.")
        method = ENDPOINTS[endpoint]
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        try:
            with anyio.fail_after(self.timeout):
                async with httpx2.AsyncClient(follow_redirects=False, trust_env=False) as client:
                    async with client.stream(
                        method, f"{self.url}/{endpoint}", headers=headers, json=payload,
                        timeout=self.timeout,
                    ) as response:
                        status = response.status_code
                        if 300 <= status < 400:
                            raise failure(502, "upstream_redirect", "The agent API returned a redirect.")
                        if response.headers.get("Content-Encoding", "identity") != "identity":
                            raise failure(502, "upstream_response", "The agent API returned an unsupported encoding.")
                        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                        if media_type.lower() != "application/json":
                            raise failure(502, "upstream_response", "The agent API returned a non-JSON response.")
                        data = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=65536):
                            if len(data) + len(chunk) > MAX_RESPONSE:
                                raise failure(502, "response_too_large", "The agent API response exceeded the size limit.")
                            data.extend(chunk)
        except (TimeoutError, httpx2.TimeoutException):
            raise failure(504, "upstream_timeout", "The agent API timed out. Check the receipt for any pending write.", True) from None
        except httpx2.HTTPError:
            raise failure(502, "upstream_unavailable", "The agent API is unreachable. Check the receipt for any pending write.", True) from None

        try:
            envelope = json.loads(data)
        except (ValueError, RecursionError):
            raise failure(502, "upstream_response", "The agent API returned invalid JSON.") from None
        if isinstance(envelope, dict):
            if 200 <= status < 300 and envelope.get("ok") is True and "data" in envelope:
                return envelope
            error = envelope.get("error")
            if status >= 400 and envelope.get("ok") is False and isinstance(error, dict):
                code, message = error.get("code"), error.get("message")
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) and isinstance(message, str):
                    return_error = {
                        "ok": False,
                        "error": {
                            "code": code if token not in code else "upstream_error",
                            "message": message[:1000].replace(token, "[redacted]"),
                            "retryable": error.get("retryable") is True,
                        },
                    }
                    for key in ("context", "revision", "request_id", "workspace"):
                        value = envelope.get(key)
                        if isinstance(value, str) and len(value) <= 512 and token not in value:
                            return_error[key] = value
                    raise AgentError(status, return_error)
        raise failure(502, "upstream_response", "The agent API returned an invalid envelope.")
