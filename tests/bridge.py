#!/usr/bin/env python3
"""Line-protocol bridge from jsdom to the real request handler without a socket.

Local requests carry a role token. Hosted requests set hosted=true and pass
the cookie header, returning HTTP headers so the browser harness can keep cookies.
"""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(HERE), str(HERE / "tests")]
import server as S
from test_event_access import request

tmp = Path(os.environ.get("KEVENOS_BRIDGE_DIR") or tempfile.mkdtemp())
S.CFG = S.load_config(str(tmp / "config.json"))
S.CFG.update(mode="desk", guest_password="SyntheticGuest8!")
S.CONTENT = S.bundled_content()
S.DB = S.open_db(str(tmp / "t.db"))
S.DATA = str(tmp)
S.event_workspace.initialize(S, tmp / "t.db", tmp)
print(json.dumps({"ready": True, "desk_pin": S.CFG["desk_pin"], "password": S.CFG["password"], "view_key": S.CFG["view_key"]}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    hosted = message.get("hosted", False)
    headers = dict(message.get("headers") or {})
    if not message.get("browser") and message["method"] == "POST":
        headers.setdefault("X-Event-Context", S.WORKSPACE.context)
    if not hosted and not message.get("nosite"):
        headers["X-Site"] = S.site_token()
    if message.get("token"):
        headers["X-Token"] = message["token"]
    with patch.dict(os.environ, {"KEVENOS_HOSTED": "1" if hosted else "0", "KEVENOS_GATEWAY_ENABLED": "0"}):
        code, body, headers = request(
            message["method"], message["path"], message.get("body"),
            message.get("cookie", ""), headers, auto_context=not message.get("browser", False),
        )
    response = {"id": message["id"], "code": code, "headers": headers}
    if isinstance(body, bytes):
        response["bytes"] = base64.b64encode(body).decode("ascii")
        if "text/" in headers.get("content-type", [""])[0]:
            response["body"] = body.decode("utf-8")
    else:
        response["body"] = body
    print(json.dumps(response), flush=True)
