"""Exercise official SDK clients on both transports using a scoped fixture API."""
import asyncio
import contextlib
import json
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from agent_client import MAX_RESPONSE, AgentClient, AgentError
from agent_mcp import GUIDE_URI, SCHEMA_URI, UUID, create_app, tools_for
from event_commands import catalog as event_catalog
from jsonschema import Draft202012Validator

TOKEN_A = "synthetic-mcp-test-alpha-only-valid-for-tests"
TOKEN_B = "synthetic-mcp-test-beta-only-valid-for-tests"
TOKEN_READER = "synthetic-mcp-quiz-reader-only-valid-for-tests"
APP_DIR = Path(__file__).resolve().parents[1]
BODY = {
    "type": "object",
    "properties": {"round": {"type": "integer", "minimum": 1}},
    "required": ["round"],
    "additionalProperties": False,
}
CLOCK = {"name": "event.clock", "description": "Advance the round", "input_schema": BODY, "risk": "confirm"}
ROUTINE = {**CLOCK, "name":"event.routine", "risk":"routine"}
STATUS = {"name": "event.status", "description": "Read status", "input_schema": {
    "type": "object", "properties": {}, "additionalProperties": False,
}}


def topic_catalog():
    catalog = event_catalog()
    return {
        group: [item for item in catalog[group] if item["name"].startswith(("quiz.", "audience.", "schedule.", "engagement."))]
        for group in ("resources", "commands")
    }


class FixtureAPI(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve(self):
        endpoint = self.path.rsplit("/", 1)[-1]
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        principal = {TOKEN_A: "alpha", TOKEN_B: "beta", TOKEN_READER: "reader"}.get(token)
        if not principal:
            self._send(401, {"ok": False, "error": {
                "code": "unauthorized", "message": f"Bad fixture token {token}", "retryable": False,
            }})
            return
        if endpoint == "capabilities":
            topics = topic_catalog() if principal in {"alpha", "reader"} else {"resources": [], "commands": []}
            value = {
                "resources": [STATUS, *topics["resources"]],
                "commands": [CLOCK, ROUTINE, *topics["commands"], next(c for c in event_catalog()["commands"] if c["name"]=="content.update_section")] if principal == "alpha" else [],
            }
        elif endpoint in {"execute", "prepare", "read", "receipts"}:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if endpoint == "execute" and body["arguments"].get("round") == 9:
                self._send(409, {"ok": False, "error": {
                    "code": "stale_revision", "message": "Read the latest round", "retryable": False,
                }, "revision": "newer"})
                return
            if endpoint == "execute" and body["command"] == "quiz.control" and body["context"] != "practice":
                self._send(409, {"ok": False, "error": {
                    "code": "workspace_changed", "message": "The selected workspace changed.", "retryable": False,
                }, "context": "practice"})
                return
            value = {"principal": principal, "received": body, "confirmation_id": "prepared"}
            if endpoint == "read" and body.get("resource") in {"schedule.retime_preview","schedule.edit_preview"}:
                value.update(can_apply=True, plan_digest="a"*64)
        else:
            value = {"principal": principal}
        self._send(200, {"ok": True, "data": value, "context": "practice", "revision": "current",
                         "request_id": "test-request"})

    def do_GET(self):
        if self.path.endswith("/status") and self.server.redirect:
            self.send_response(302)
            self.send_header("Location", self.server.redirect)
            self.end_headers()
            return
        if self.path.endswith("/status") and self.server.oversize:
            self._send(200, {"ok": True, "data": "x" * MAX_RESPONSE})
            return
        self._serve()

    do_POST = _serve


class MCPTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FixtureAPI)
        cls.api.redirect = ""
        cls.api.oversize = False
        cls.api_thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.base = f"http://127.0.0.1:{cls.api.server_port}"
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.mcp_url = f"http://127.0.0.1:{cls.listener.getsockname()[1]}/mcp"
        cls.server = uvicorn.Server(uvicorn.Config(create_app(cls.base), log_level="error", access_log=False))
        cls.remote = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True)
        cls.remote.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Fixture MCP server did not start.")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.remote.join(timeout=10)
        cls.listener.close()
        cls.api.shutdown()
        cls.api.server_close()
        cls.api_thread.join(timeout=10)

    @contextlib.asynccontextmanager
    async def remote_client(self, token):
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False) as client:
            async with streamable_http_client(self.mcp_url, http_client=client) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.server_info.name, "kevenos")
                    yield session

    async def exercise(self, session):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        clock = tools["event_clock"]
        self.assertTrue(clock.annotations.destructive_hint)
        self.assertTrue(clock.annotations.idempotent_hint)
        self.assertEqual(clock.input_schema["properties"]["arguments"], BODY)
        self.assertIn("confirmation_id", clock.input_schema["required"])
        resources = {str(resource.uri) for resource in (await session.list_resources()).resources}
        self.assertEqual(resources, {GUIDE_URI, SCHEMA_URI})
        guide = await session.read_resource(GUIDE_URI)
        self.assertIn("KevenOS agent operator guide", guide.contents[0].text)
        schema = await session.read_resource(SCHEMA_URI)
        self.assertEqual(json.loads(schema.contents[0].text)["commands"][0], CLOCK)
        self.assertIn("operate_final", {p.name for p in (await session.list_prompts()).prompts})
        self.assertIn("original finalist scenarios", (await session.get_prompt("operate_final")).messages[0].content.text)
        status = await session.call_tool("event_status")
        self.assertEqual(status.structured_content["data"]["principal"], "alpha")
        write = {"arguments": {"round": 2}, "context": "practice", "revision": "current"}
        prepared = await session.call_tool("agent_prepare", {"command": "event.clock", **write})
        self.assertFalse(prepared.is_error)
        next_call = prepared.structured_content["data"]["next_call"]
        self.assertEqual(next_call["tool"], "event_clock")
        self.assertEqual(next_call["arguments"]["arguments"], write["arguments"])
        self.assertEqual(next_call["arguments"]["context"], write["context"])
        self.assertEqual(next_call["arguments"]["revision"], write["revision"])
        self.assertEqual(next_call["arguments"]["confirm"], "EXECUTE")
        self.assertEqual(next_call["arguments"]["confirmation_id"], prepared.structured_content["data"]["confirmation_id"])
        self.assertEqual(next_call["arguments"]["idempotency_key"], prepared.structured_content["data"]["idempotency_key"])
        self.assertIn("not executed", prepared.structured_content["data"]["instruction"])
        write.update(confirmation_id=prepared.structured_content["data"]["confirmation_id"], confirm="EXECUTE",
                     idempotency_key="b0341f8a-3057-4c6b-ab53-ea20be2bfc51")  # gitleaks:allow -- synthetic idempotency identifier
        updated = await session.call_tool("event_clock", write)
        self.assertFalse(updated.is_error)
        self.assertEqual(updated.structured_content["data"]["received"]["command"], "event.clock")
        invalid = await session.call_tool("event_clock", {**write, "arguments": {"round": True}})
        self.assertTrue(invalid.is_error)
        self.assertEqual(invalid.structured_content["error"]["code"], "invalid_arguments")
        stale = await session.call_tool("event_clock", {**write, "arguments": {"round": 9}})
        self.assertTrue(stale.is_error)
        self.assertEqual(stale.structured_content["error"]["code"], "stale_revision")
        self.assertEqual(stale.structured_content["revision"], "newer")
        receipt = await session.call_tool("agent_receipt", {"idempotency_key": write["idempotency_key"]})
        self.assertFalse(receipt.is_error)
        self.assertEqual(receipt.structured_content["data"]["received"]["idempotency_key"], write["idempotency_key"])

    async def exercise_quiz(self, session):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        self.assertTrue(tools["quiz_state"].annotations.read_only_hint)
        self.assertTrue(tools["quiz_analytics"].annotations.read_only_hint)
        self.assertTrue(tools["quiz_control"].annotations.destructive_hint)
        self.assertTrue(tools["quiz_control"].annotations.open_world_hint)
        self.assertTrue(tools["engagement_state"].annotations.read_only_hint)
        self.assertTrue(tools["engagement_control"].annotations.destructive_hint)
        self.assertTrue(tools["engagement_control"].annotations.open_world_hint)
        self.assertTrue(tools["engagement_settings"].annotations.destructive_hint)
        self.assertTrue(tools["engagement_settings"].annotations.open_world_hint)
        self.assertIn("operate_quiz", {p.name for p in (await session.list_prompts()).prompts})
        prompt = (await session.get_prompt("operate_quiz")).messages[0].content.text
        self.assertIn("quiz_analytics", prompt)
        self.assertIn("agent_prepare", prompt)

        engagement = await session.call_tool("engagement_state")
        self.assertFalse(engagement.is_error)
        self.assertEqual(engagement.structured_content["data"]["received"]["resource"], "engagement.state")
        draft = {"arguments":{"action":"save", "revision":0, "payload":{"title":"Poll", "kind":"poll", "questions":[
            {"type":"single", "prompt":"Choose", "options":["A","B"]}]}}, "context":"practice", "revision":"current"}
        prepared_draft = await session.call_tool("agent_prepare", {"command":"engagement.control", **draft})
        self.assertFalse(prepared_draft.is_error)
        sent = await session.call_tool("engagement_control", {**draft, "confirm":"EXECUTE",
            "confirmation_id":prepared_draft.structured_content["data"]["confirmation_id"],
            "idempotency_key":prepared_draft.structured_content["data"]["idempotency_key"]})
        self.assertFalse(sent.is_error)
        settings = {"arguments":{"revision":0,"enabled":False},"context":"practice","revision":"current"}
        prepared_settings = await session.call_tool("agent_prepare", {"command":"engagement.settings", **settings})
        self.assertFalse(prepared_settings.is_error)
        changed = await session.call_tool("engagement_settings", {**settings, "confirm":"EXECUTE",
            "confirmation_id":prepared_settings.structured_content["data"]["confirmation_id"],
            "idempotency_key":prepared_settings.structured_content["data"]["idempotency_key"]})
        self.assertFalse(changed.is_error)

        state = await session.call_tool("quiz_state")
        self.assertFalse(state.is_error)
        result = state.structured_content
        self.assertEqual(result["data"]["received"], {"resource": "quiz.state", "arguments": {}})
        analysis = await session.call_tool("quiz_analytics")
        self.assertFalse(analysis.is_error)
        self.assertEqual(analysis.structured_content["data"]["received"]["resource"], "quiz.analytics")
        original = {
            "arguments": {"action": "settings", "enabled": False},
            "context": result["context"], "revision": result["revision"],
        }
        key = "b0341f8a-3057-4c6b-ab53-ea20be2bfc52"  # gitleaks:allow -- synthetic idempotency identifier
        unconfirmed = await session.call_tool("quiz_control", {**original, "idempotency_key": key})
        self.assertTrue(unconfirmed.is_error)
        prepared = await session.call_tool("agent_prepare", {"command": "quiz.control", **original})
        self.assertFalse(prepared.is_error)
        actions = {
            **original, "idempotency_key": key, "confirm": "EXECUTE",
            "confirmation_id": prepared.structured_content["data"]["confirmation_id"],
        }
        updated = await session.call_tool("quiz_control", actions)
        self.assertFalse(updated.is_error)
        self.assertEqual(updated.structured_content["data"]["received"], {"command": "quiz.control", **actions})
        wrong_workspace = await session.call_tool("quiz_control", {**actions, "context": "live"})
        self.assertTrue(wrong_workspace.is_error)
        self.assertEqual(wrong_workspace.structured_content["error"]["code"], "workspace_changed")

    async def test_real_catalog_tools_preserve_schemas_and_annotations(self):
        catalog = event_catalog()
        tools = tools_for(catalog)
        self.assertEqual(len(tools), len(catalog["resources"]) + len(catalog["commands"]) + 4)
        for item in catalog["resources"]:
            name = item["name"].replace(".", "_")
            definition, endpoint, action = tools[name]
            self.assertEqual((endpoint, action), ("read", item["name"]))
            self.assertEqual(definition.input_schema, item["input_schema"])
            self.assertTrue(definition.annotations.read_only_hint)
            Draft202012Validator.check_schema(definition.input_schema)
        for item in catalog["commands"]:
            name = item["name"].replace(".", "_")
            definition, endpoint, action = tools[name]
            self.assertEqual((endpoint, action), ("execute", item["name"]))
            self.assertEqual(definition.input_schema["properties"]["arguments"], item["input_schema"])
            self.assertEqual("confirmation_id" in definition.input_schema["required"], item["risk"] == "confirm")
            Draft202012Validator.check_schema(definition.input_schema)
        Draft202012Validator.check_schema(tools["agent_prepare"][0].input_schema)
        for name in ("notifications_send", "credentials_issue", "credentials_reset"):
            self.assertTrue(tools[name][0].annotations.open_world_hint)

    async def test_stdio_sdk(self):
        config = StdioServerParameters(
            command=sys.executable, args=[str(APP_DIR / "agent_mcp.py"), "--api-url", self.base],
            env={"KEVENOS_AGENT_TOKEN": TOKEN_A},
        )
        async with Client(config, mode="legacy", read_timeout_seconds=10) as client:
            await self.exercise(client)
            await self.exercise_quiz(client)

    async def test_remote_sdk(self):
        async with self.remote_client(TOKEN_A) as client:
            await self.exercise(client)
            await self.exercise_quiz(client)

    async def test_audience_tools_require_confirmation_on_sdk_transport(self):
        async with self.remote_client(TOKEN_A) as client:
            tools = {tool.name:tool for tool in (await client.list_tools()).tools}
            self.assertTrue(tools["audience_state"].annotations.read_only_hint)
            self.assertTrue(tools["audience_control"].annotations.open_world_hint)
            prompt = (await client.get_prompt("operate_audience")).messages[0].content.text
            self.assertIn("agent_prepare",prompt)
            state = (await client.call_tool("audience_state")).structured_content
            payload = {"arguments":{"action":"save","pack":{"title":"Sample",
                       "questions":[{"type":"rating","prompt":"How useful was it?"}]}},
                       "context":state["context"],"revision":state["revision"]}
            key = "5d97132e-81f1-41cd-b640-a2d7ea87292e"
            self.assertTrue((await client.call_tool("audience_control",
                {**payload,"idempotency_key":key})).is_error)
            prepared = await client.call_tool("agent_prepare",{"command":"audience.control",**payload})
            self.assertFalse(prepared.is_error)
            self.assertTrue(Draft202012Validator(UUID).is_valid(prepared.structured_content["data"]["idempotency_key"]))
            execution = {**payload,"idempotency_key":key,"confirm":"EXECUTE",
                         "confirmation_id":prepared.structured_content["data"]["confirmation_id"]}
            result = await client.call_tool("audience_control",execution)
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["data"]["received"],
                             {"command":"audience.control",**execution})
        async with self.remote_client(TOKEN_READER) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            self.assertIn("audience_state",tools)
            self.assertNotIn("audience_control",tools)
        async with self.remote_client(TOKEN_B) as client:
            self.assertNotIn("operate_audience",{p.name for p in (await client.list_prompts()).prompts})

    async def test_timing_preview_returns_complete_prepare_handoff(self):
        async with self.remote_client(TOKEN_A) as client:
            for tool,arguments,command in [
                ("schedule_retime_preview",{"change":{"id":"a"*32,"duration_minutes":20},"shift_following":True},"schedule.retime"),
                ("schedule_edit_preview",{"edit":{"action":"insert","item":{"title":"Break","min":10,"scene":"info"},"before_id":"a"*32}},"schedule.edit")]:
                result = await client.call_tool(tool,arguments)
                self.assertFalse(result.is_error,result)
                call = result.structured_content["data"]["prepare_call"]
                self.assertEqual(call["tool"],"agent_prepare")
                self.assertEqual(call["arguments"],{"command":command,"arguments":{**arguments,"plan_digest":"a"*64},"context":"practice","revision":"current"})
                prepared = await client.call_tool(call["tool"],call["arguments"])
                self.assertFalse(prepared.is_error)
            invalid = await client.call_tool("agent_prepare",{"arguments":{},"context":"practice","revision":"current"})
            self.assertIn("missing command",invalid.content[0].text)
            invalid_read = await client.call_tool("schedule_edit_preview",{"action":"insert"})
            self.assertIn("resource='schedule.edit_preview'",invalid_read.content[0].text)

    async def test_large_prepare_keeps_execution_envelope_in_text(self):
        async with self.remote_client(TOKEN_A) as client:
            arguments = {"section":"event", "value":{k:"x"*3000 for k in
                         ("name","headline","date","time","venue","layout","goal","who")}}
            result = await client.call_tool("agent_prepare", {"command":"content.update_section",
                "arguments":arguments, "context":"practice", "revision":"current"})
            self.assertFalse(result.is_error)
            text = json.loads(result.content[0].text)
            self.assertLess(len(result.content[0].text.encode()), 20_000)
            self.assertEqual(text["data"]["confirmation_id"], result.structured_content["data"]["confirmation_id"])
            call = text["data"]["next_call"]
            self.assertEqual(call["tool"], "content_update_section")
            self.assertTrue(call["reuse_prepared_arguments"])
            self.assertNotIn("arguments",call["arguments"])
            execution = {**call["arguments"],"arguments":arguments}
            self.assertEqual(execution,result.structured_content["data"]["next_call"]["arguments"])

    async def test_focused_capabilities_and_schema_recovery(self):
        async with self.remote_client(TOKEN_A) as client:
            result = await client.call_tool("agent_capabilities", {"command":"audience.control"})
            self.assertFalse(result.is_error)
            self.assertEqual([x["name"] for x in result.structured_content["data"]["commands"]], ["audience.control"])
            self.assertEqual(result.structured_content["data"]["resources"], [])
            self.assertIn('"action"', result.content[0].text)
            self.assertLess(len(result.content[0].text.encode()), 20_000)
            invalid = await client.call_tool("audience_control", {"arguments":{"action":"send_pack"}})
            self.assertTrue(invalid.is_error)
            self.assertIn("agent_capabilities", invalid.structured_content["error"]["message"])
            self.assertIn("audience.control", invalid.structured_content["error"]["message"])
            routine = await client.call_tool("event_routine", {"arguments":{"round":True}})
            self.assertTrue(routine.is_error)
            self.assertIn("does not use agent_prepare", routine.structured_content["error"]["message"])
            bad_key = await client.call_tool("event_clock", {
                "arguments":{"round":1}, "context":"practice", "revision":"current",
                "confirmation_id":"prepared", "confirm":"EXECUTE", "idempotency_key":"private-label-should-not-echo"})
            message = bad_key.structured_content["error"]["message"]
            self.assertIn("idempotency_key: pattern", message)
            self.assertIn("UUID", message)
            self.assertNotIn("private-label-should-not-echo", message)


        async with self.remote_client(TOKEN_READER) as client:
            unavailable = await client.call_tool("agent_capabilities", {"command":"audience.control"})
            self.assertTrue(unavailable.is_error)
            self.assertNotIn('"pack"', unavailable.content[0].text)

    def test_prepare_and_audience_fields_survive_client_combinator_stripping(self):
        tools = tools_for(event_catalog())
        prepare = dict(tools["agent_prepare"][0].input_schema)
        # Hermes strips top-level combinators before forwarding tools to a model.
        for key in ("oneOf", "anyOf", "allOf"):
            prepare.pop(key, None)
        self.assertEqual(set(prepare["properties"]), {"command","arguments","context","revision"})
        self.assertIn("audience.control", prepare["properties"]["command"]["enum"])
        edit = tools["schedule_edit_preview"][0].input_schema["properties"]["edit"]
        self.assertEqual(edit["type"],"object")
        self.assertEqual(set(edit["properties"]),{"action","id","before_id","item","changes"})
        self.assertEqual(set(edit["properties"]["action"]["enum"]),{"insert","update","remove","move"})
        self.assertFalse(Draft202012Validator(edit).is_valid({"action":"remove","id":"a"*32,"item":{"title":"Break","min":10}}))
        control = tools["audience_control"][0].input_schema["properties"]["arguments"]
        self.assertEqual(set(control["properties"]), {"action","pack","packId","minutes","questionId","results"})
        question = control["properties"]["pack"]["properties"]["questions"]["items"]
        self.assertEqual(question["properties"]["type"]["enum"], ["choice","rating","text"])
        validator = Draft202012Validator(control)
        self.assertTrue(validator.is_valid({"action":"send","packId":"a"*32,"minutes":5}))
        self.assertTrue(validator.is_valid({"action":"show","packId":"a"*32,"questionId":"b"*32,"results":True}))
        self.assertTrue(validator.is_valid({"action":"hide"}))
        self.assertFalse(validator.is_valid({"action":"show","packId":"a"*32}))
        for bad in ({"action":"send_pack"}, {"action":"send"},
                    {"action":"save","pack":{"title":"Test","questions":[{"type":"choice","prompt":"Q?"}]}},
                    {"action":"send","packId":"a"*32,"minutes":121},
                    {"action":"close","packId":"a"*32,"minutes":5}):
            self.assertFalse(validator.is_valid(bad), bad)

    async def test_remote_requires_auth_before_discovery(self):
        async with httpx2.AsyncClient(trust_env=False) as client:
            for method in ("initialize", "server/discover", "tools/list", "resources/list", "prompts/list", "ping"):
                response = await client.post(self.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": method})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            invalid = "synthetic-invalid-token-only-valid-for-tests"
            response = await client.post(self.mcp_url, headers={"Authorization": f"Bearer {invalid}"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(invalid, response.text)
            response = await client.get(self.mcp_url)
            self.assertEqual(response.status_code, 401)

    async def test_quiz_reader_cannot_prepare_or_control(self):
        async with self.remote_client(TOKEN_READER) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            self.assertTrue({"quiz_state", "quiz_analytics"} <= tools)
            self.assertTrue({"quiz_control", "agent_prepare"}.isdisjoint(tools))
            analytics = await client.call_tool("quiz_analytics")
            self.assertFalse(analytics.is_error)
            denied = await client.call_tool("quiz_control")
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.structured_content["error"]["code"], "tool_unavailable")

    async def test_remote_token_isolation(self):
        async def read(token, principal):
            async with self.remote_client(token) as client:
                for _ in range(4):
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertEqual("event_clock" in tools, principal == "alpha")
                    result = await client.call_tool("agent_status")
                    self.assertEqual(result.structured_content["data"]["principal"], principal)
                    self.assertNotIn(token, result.content[0].text)
                    if principal == "beta":
                        self.assertTrue(tools.isdisjoint({"quiz_state", "quiz_analytics", "quiz_control"}))
                        denied = await client.call_tool("event_clock")
                        self.assertTrue(denied.is_error)
                        self.assertEqual(denied.structured_content["error"]["code"], "tool_unavailable")
                        quiz_denied = await client.call_tool("quiz_analytics")
                        self.assertTrue(quiz_denied.is_error)
                        self.assertEqual(quiz_denied.structured_content["error"]["code"], "tool_unavailable")
                if principal == "beta":
                    self.assertNotIn("operate_quiz", {p.name for p in (await client.list_prompts()).prompts})
                    with self.assertRaises(MCPError):
                        await client.get_prompt("operate_quiz")
        await asyncio.gather(read(TOKEN_A, "alpha"), read(TOKEN_B, "beta"))

    async def test_client_rejects_nonloopback_http_and_redacts_errors(self):
        with self.assertRaises(ValueError):
            AgentClient("http://example.org")
        with self.assertRaises(ValueError):
            AgentClient("https://example.org?bad=query")
        with self.assertRaises(AgentError) as denied:
            await AgentClient(self.base).request("status", "synthetic-invalid-token-only-valid-for-tests")
        self.assertEqual(denied.exception.status, 401)
        self.assertNotIn("synthetic-invalid-token-only-valid-for-tests", str(denied.exception))


    async def test_api_client_rejects_redirect_and_oversized_response(self):
        self.api.redirect = self.mcp_url
        try:
            with self.assertRaises(AgentError) as redirected:
                await AgentClient(self.base).request("status", TOKEN_A)
            self.assertEqual(redirected.exception.envelope["error"]["code"], "upstream_redirect")
        finally:
            self.api.redirect = ""
        self.api.oversize = True
        try:
            with self.assertRaises(AgentError) as oversized:
                await AgentClient(self.base).request("status", TOKEN_A)
            self.assertEqual(oversized.exception.envelope["error"]["code"], "response_too_large")
        finally:
            self.api.oversize = False


if __name__ == "__main__":
    unittest.main()
