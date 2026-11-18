"""KevenOS MCP server for local stdio and hosted Streamable HTTP."""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
import json
import os
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_client import AgentClient, AgentError, DEFAULT_URL, failure, token_from_env, valid_token

GUIDE_URI = "kevenos://agent/guide"
SCHEMA_URI = "kevenos://agent/schema"
GUIDE_FILE = Path(__file__).resolve().parent / "docs" / "AGENT-GUIDE.md"
REQUEST_TOKEN: ContextVar[str] = ContextVar("kevenos_request_token", default="")
UUID = {"description": "Use the idempotency_key returned by agent_prepare for a prepared command. Otherwise generate a fresh UUID with 32 hexadecimal digits and hyphens in 8-4-4-4-12 form. Generate a new UUID for each write; never use a label such as poll-save-1 or a pack ID. Reuse only for an identical retry.", "type": "string", "pattern": "^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"}
CONTEXT_FIELDS = {
    "context": {"type": "string", "minLength": 1, "description": "Current event workspace context."},
    "revision": {"type": "string", "minLength": 1, "description": "Revision returned by the latest relevant read."},
}
CONFIRM_FIELDS = {
    "confirmation_id": {"type": "string", "minLength": 1, "description": "Prepared ID for this exact command and revision."},
    "confirm": {"const": "EXECUTE", "description": "Use after the event operator explicitly approves the prepared action."},
}
WORKFLOWS = {
    "prepare_event_day": (
        "Plan the day in the selected workspace",
        "Read the guide, then check status and capabilities. Inspect schedule, content, roster, check-ins, "
        "assignments and staff readiness with the tools permitted by this credential. Summarize conflicts and "
        "propose changes. Verify workspace and current revision, then ask the operator before consequential actions.",
    ),
    "operate_preliminaries": (
        "Check the desk, assignments and clocks before a preliminary round",
        "Inspect current round, station assignments, check-ins, program and clocks. Report what is running and "
        "what should happen next. Keep the current workspace. If asked to act, confirm the station and round and "
        "read back the desk state after the approved operation.",
    ),
    "operate_final": (
        "Check Final readiness and publishing barriers",
        "Read Final and scoring status. Keep the original finalist scenarios and verify the panel, sheets and clock. "
        "Surface any missing sheets or tie decision to the operator. Ask for explicit approval before overrides, "
        "tie resolution, publishing or reopening. Publish only through the dedicated Final publish operation.",
    ),
}
QUIZ_WORKFLOW = (
    "Operate the event quiz in the selected workspace",
    "Call agent_status and quiz_state. Confirm the workspace, agenda section and released prompts. "
    "Use quiz_analytics for response counts, answer distributions and Desk activity. Report percentages "
    "among respondents and keep Organizer analytics private. If the operator requests a change and quiz_control "
    "is available, prepare the exact action with agent_prepare using the current context and revision. "
    "Confirm the question, audience and effect with the operator. Send only the approved prompt or set, "
    "then read quiz_state again. Keep the current workspace and leave hall publication to a separately approved action.",
)


AUDIENCE_WORKFLOW = (
    "Prepare or run unscored Audience questions in the selected workspace",
    "Read agent_status and audience_state; confirm the workspace and the request. "
    "Draft one to ten multiple choice, 1–5 rating or free-text questions. Only choice questions "
    "use options; Audience questions have no correct choices or points. Propose a save via "
    "agent_prepare, then use audience_control after confirmation. Keep IDs of retained questions "
    "when editing a draft and omit IDs for new questions. Sending requires separate confirmation; "
    "specify the duration in minutes (default 5). Read state after execution and ask for separate "
    "confirmation before releasing aggregate results. Never include participant identities.",
)


def object_schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object", "properties": properties, "required": list(required),
        "additionalProperties": False,
    }


def tool(
    name: str, description: str, schema: dict[str, Any], *,
    read_only: bool = True, destructive: bool = False, idempotent: bool = True,
    open_world: bool = False,
) -> types.Tool:
    return types.Tool(
        name=name, description=description, input_schema=schema,
        annotations=types.ToolAnnotations(
            read_only_hint=read_only, destructive_hint=destructive,
            idempotent_hint=idempotent, open_world_hint=open_world,
        ),
    )


def tools_for(catalog: dict[str, Any]) -> dict[str, tuple[types.Tool, str, str | None]]:
    entries = {
        "agent_status": (
            tool("agent_status", "Read the current workspace, revision and agent authority.", object_schema({})),
            "status", None,
        ),
        "agent_capabilities": (
            tool("agent_capabilities", "Discover permitted schemas. Pass command (e.g. audience.control) or resource for a focused, text-readable schema before constructing arguments.",
                 object_schema({"command": {"type":"string", "minLength":1, "maxLength":100},
                                "resource": {"type":"string", "minLength":1, "maxLength":100}})),
            "capabilities", None,
        ),
        "agent_receipt": (
            tool("agent_receipt", "Look up your write receipt by its original idempotency key.",
                 object_schema({"idempotency_key": UUID}, "idempotency_key")),
            "receipts", None,
        ),
    }
    preparations = []
    for resource in catalog["resources"]:
        name = resource["name"].replace(".", "_")
        entries[name] = (
            tool(name, resource["description"], resource["input_schema"]), "read", resource["name"],
        )
    for command in catalog["commands"]:
        name = command["name"].replace(".", "_")
        needs_confirmation = command["risk"] == "confirm"
        arguments = {"arguments": command["input_schema"]}
        fields = {**arguments, **CONTEXT_FIELDS, "idempotency_key": UUID}
        if needs_confirmation:
            fields.update(CONFIRM_FIELDS)
            preparations.append(object_schema({
                "command": {"const": command["name"]}, **arguments, **CONTEXT_FIELDS,
            }, "command", "arguments", *CONTEXT_FIELDS))
        entries[name] = (
            tool(
                name, command["description"] + (
                    " Required sequence: agent_prepare first, then execute using its real returned confirmation_id, "
                    "unchanged arguments/context/revision, confirm=EXECUTE and its returned idempotency_key. "
                    "A Hermes tool approval does not replace this server preparation."
                    if needs_confirmation else ""), object_schema(fields, *fields),
                read_only=False, destructive=needs_confirmation,
                open_world=command["name"].startswith(("notifications.", "credentials.issue", "credentials.reset"))
                or command["name"] in {"quiz.control","audience.control"},
            ),
            "execute", command["name"],
        )
    if preparations:
        entries["agent_prepare"] = (
            tool(
                "agent_prepare",
                "Prepare a consequential command with its dotted name and exact arguments. Read agent_capabilities(command=...) for its argument schema. Use the returned confirmation_id for execution after operator approval; never invent one.",
                {**object_schema({
                    "command": {"type":"string", "enum":[item["properties"]["command"]["const"] for item in preparations]},
                    "arguments": {"type":"object", "description":"Exact command payload from agent_capabilities(command=...). No context or confirmation fields inside this object."},
                    **CONTEXT_FIELDS,
                }, "command", "arguments", *CONTEXT_FIELDS), "oneOf": preparations},
                read_only=False, idempotent=False,
            ),
            "prepare", None,
        )
    return entries


def build_server(api: AgentClient, local_token: str = "") -> Server:
    async def request(endpoint, payload=None):
        return await api.request(endpoint, local_token or REQUEST_TOKEN.get(), payload)

    async def catalog():
        return (await request("capabilities"))["data"]

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[entry[0] for entry in tools_for(await catalog()).values()])

    async def call_tool(ctx, params):
        try:
            # Do not cache another principal's capabilities or dispatch table.
            entry = tools_for(await catalog()).get(params.name)
            if entry is None:
                raise failure(403, "tool_unavailable", "This tool is unavailable for the current credential and workspace.")
            definition, endpoint, action = entry
            args = params.arguments or {}
            errors = list(Draft202012Validator(definition.input_schema).iter_errors(args))
            if errors:
                # Report schema paths/keywords only, never values (which can contain credentials).
                problems = []
                for error in errors[:5]:
                    path = ".".join(str(part) for part in error.absolute_path) or "root"
                    if error.validator == "required" and isinstance(error.instance, dict):
                        missing = sorted(set(error.validator_value) - set(error.instance))
                        problems.append(f"{path}: missing " + ", ".join(missing))
                    else:
                        problems.append(f"{path}: {error.validator}")
                detail = "Invalid fields (schema rules): " + "; ".join(problems) + ". "
                if any(list(error.absolute_path) == ["idempotency_key"] for error in errors):
                    detail += "idempotency_key must be a fresh UUID in 8-4-4-4-12 hexadecimal form, not a descriptive label. "
                raise failure(400, "invalid_arguments",
                              "Tool arguments do not match the advertised schema. " + detail
                              + (f"Call agent_capabilities with command={action!r} for the exact arguments; "
                                 + ("prepare the exact payload with agent_prepare before executing with its returned confirmation_id."
                                    if "confirmation_id" in definition.input_schema.get("required", []) else
                                    "retry the corrected arguments with current context, revision and a fresh idempotency key. This routine command does not use agent_prepare.")
                                 if endpoint == "execute" else
                                 f"Call agent_capabilities with resource={action!r} for this read tool's exact schema."
                                 if endpoint == "read" else
                                 "Call agent_capabilities with the dotted command or resource name for a focused schema. "
                                 "agent_prepare requires command, arguments, context and revision."))
            if endpoint == "read":
                args = {"resource": action, "arguments": args}
            elif endpoint == "execute":
                args = {"command": action, **args}
            result = await request(endpoint, args if endpoint not in {"status", "capabilities"} else None)
            timing_commands = {"schedule.retime_preview":"schedule.retime", "schedule.edit_preview":"schedule.edit"}
            if endpoint == "read" and action in timing_commands and result.get("ok") and result["data"].get("can_apply"):
                proposal = {"command": timing_commands[action],
                            "arguments": {**args["arguments"], "plan_digest": result["data"]["plan_digest"]},
                            **{field: result[field] for field in CONTEXT_FIELDS}}
                result = {**result, "data": {**result["data"],
                    "prepare_call": {"tool":"agent_prepare", "arguments":proposal},
                    "execution_hint":"Preview only; no schedule change was made. After approval of these timings, "
                                     "call prepare_call.tool with prepare_call.arguments, then follow its next_call. "
                                     "Do not substitute the display label or change the approved plan."}}
            if endpoint == "prepare" and result.get("ok"):
                # An execution key is convenience data, never authority. The API still binds
                # confirmation to the exact payload and checks all write receipts itself.
                key = str(uuid.uuid4())
                execution = {field: args[field] for field in ("arguments", "context", "revision")}
                execution.update(confirmation_id=result["data"]["confirmation_id"],
                                 confirm="EXECUTE", idempotency_key=key)
                result = {**result, "data": {**result["data"], "idempotency_key": key,
                    "instruction": "Prepared only; not executed. If the operator already approved this exact effect, "
                                   "call next_call.tool with next_call.arguments now, using your client's service prefix. "
                                   "Do not call agent_prepare again or look up a receipt before execution. "
                                   "Otherwise obtain approval for the effect first. Read affected state after execution.",
                    "next_call": {"tool": args["command"].replace(".", "_"), "arguments": execution}}}
            if endpoint == "capabilities" and args:
                data = result["data"]
                selected = {"resources": [], "commands": []}
                for selector, group in (("command", "commands"), ("resource", "resources")):
                    if selector in args:
                        selected[group] = [item for item in data[group] if item["name"] == args[selector]]
                        if not selected[group]:
                            raise failure(403, "tool_unavailable", "Requested schema is unavailable for the current credential and workspace.")
                result = {**result, "data": selected}
        except AgentError as error:
            result = error.envelope
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > 20_000:
            summary = {key: result[key] for key in ("ok", "context", "revision", "request_id", "workspace") if key in result}
            if endpoint == "prepare" and result.get("ok"):
                data = result["data"]
                call = data["next_call"]
                summary["data"] = {key: data[key] for key in
                    ("command", "confirmation_id", "expires_at", "idempotency_key") if key in data}
                summary["data"]["next_call"] = {"tool": call["tool"], "reuse_prepared_arguments": True,
                    "arguments": {key: value for key, value in call["arguments"].items() if key != "arguments"}}
                summary["data"]["instruction"] = ("Prepared only; not executed. The large payload was omitted from this text. "
                    "Use next_call.arguments plus arguments equal to your exact original prepared arguments. "
                    "Call next_call.tool once the effect is approved, then read back state. "
                    "The full execution payload is also in structuredContent. Do not prepare again.")
            else:
                summary["message"] = "Read structuredContent for the full result or narrow the read for a text-only client."
            text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=result,
            is_error=result["ok"] is False,
        )

    async def list_resources(ctx, params):
        await request("status")
        return types.ListResourcesResult(resources=[
            types.Resource(name="agent-guide", uri=GUIDE_URI, mime_type="text/markdown",
                           description="Operator guide for running the event through MCP or the HTTP API."),
            types.Resource(name="agent-schema", uri=SCHEMA_URI, mime_type="application/json",
                           description="Authorized API resources, commands and argument schemas."),
        ])

    async def read_resource(ctx, params):
        uri = str(params.uri)
        if uri == GUIDE_URI:
            await request("status")
            media_type, value = "text/markdown", GUIDE_FILE.read_text(encoding="utf-8")
        elif uri == SCHEMA_URI:
            media_type, value = "application/json", json.dumps(await catalog(), ensure_ascii=False)
        else:
            raise ValueError("Unknown KevenOS resource.")
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type=media_type, text=value)],
        )

    async def workflows():
        resources = {resource["name"] for resource in (await catalog())["resources"]}
        current = dict(WORKFLOWS)
        if {"quiz.state", "quiz.analytics"} <= resources:
            current["operate_quiz"] = QUIZ_WORKFLOW
        if "audience.state" in resources:
            current["operate_audience"] = AUDIENCE_WORKFLOW
        return current

    async def list_prompts(ctx, params):
        return types.ListPromptsResult(prompts=[
            types.Prompt(name=name, description=description)
            for name, (description, _) in (await workflows()).items()
        ])

    async def get_prompt(ctx, params):
        visible = await workflows()
        if params.name not in visible or params.arguments:
            raise MCPError(types.INVALID_PARAMS, "Unknown KevenOS prompt or invalid arguments.")
        description, message = visible[params.name]
        return types.GetPromptResult(description=description, messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=message)),
        ])

    return Server(
        "kevenos", title="KevenOS", version="5.7.1",
        instructions="Read the agent guide and inspect status before acting. Use the current workspace "
                     "context and revision, and obtain explicit approval for consequential operations.",
        on_list_tools=list_tools, on_call_tool=call_tool,
        on_list_resources=list_resources, on_read_resource=read_resource,
        on_list_prompts=list_prompts, on_get_prompt=get_prompt,
    )


def create_app(api_base: str):
    api = AgentClient(api_base)
    app = build_server(api).streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=400_000,
    )

    async def authenticated_app(scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/mcp":
            await app(scope, receive, send)
            return
        headers = Request(scope).headers.getlist("authorization")
        scheme, _, token = headers[0].partition(" ") if len(headers) == 1 else ("", "", "")
        try:
            if scheme.lower() != "bearer" or not valid_token(token):
                raise failure(401, "unauthorized", "A valid bearer credential is required.")
            await api.request("status", token)
        except AgentError as error:
            response = JSONResponse(
                error.envelope, status_code=error.status,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"}
                if error.status == 401 else {"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        async def no_store(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (name, value) for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        reset = REQUEST_TOKEN.set(token)
        try:
            await app(scope, receive, no_store)
        finally:
            REQUEST_TOKEN.reset(reset)

    return authenticated_app


def start_remote(api_base: str, port: int = 8081) -> None:
    uvicorn.run(create_app(api_base), host="127.0.0.1", port=port, access_log=False, proxy_headers=False)


async def run_stdio(api_base: str, token: str) -> None:
    api = AgentClient(api_base)
    await api.request("status", token)
    server = build_server(api, token)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("KEVENOS_AGENT_API_URL", DEFAULT_URL))
    args = parser.parse_args()
    try:
        asyncio.run(run_stdio(args.api_url, token_from_env()))
    except (AgentError, ValueError) as error:
        parser.exit(1, f"KevenOS MCP: {error}\n")


if __name__ == "__main__":
    main()
