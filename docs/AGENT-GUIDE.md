# KevenOS agent operator guide

This package exposes an optional MCP and HTTP API for an event operator's
agent. It contains no production credentials, event records, attendee data, or
deployment addresses.

## Connect

- Local MCP: run `python agent_mcp.py --transport stdio` from this directory.
- Remote MCP: configure the agent gateway and application grant file, route its
  authenticated endpoint, then connect to `${KEVENOS_PUBLIC_URL}/mcp` with the
  corresponding bearer token.
- HTTP API: use `${KEVENOS_PUBLIC_URL}/api/v1/agent` with the same scoped token.

Set the public URL, token grants, database path, mail, push and AI settings with
environment variables documented in the package deployment guide. Never put
tokens in source files or agent prompts.

## Safe operating sequence

1. Read `agent_status` and confirm the selected workspace.
2. Read the relevant schedule, roster, judging, quiz, or audience state.
3. Explain the proposed change and its effect to the operator.
4. Call `agent_prepare` with the current context and revision.
5. Execute only after the operator gives the explicit confirmation required by
   the prepared action.
6. Read the affected state again and report the resulting revision.

Treat attendee identities, scores, responses, notes and access credentials as
private event data. Do not copy them into logs or external services. Use a
dedicated least-privilege token for each agent and revoke it after the event.

## Deployment boundary

The reusable package provides schemas and controls, not a configured agent.
The deployer must supply a public origin, TLS termination, token grants and any
optional AI gateway. Model calls are disabled unless an AI base URL and key are
configured explicitly.

## Experimental Engagement Hub

When `KEVENOS_ENGAGEMENT_V2=1`, the `engagement.state` resource reads a page of
activities or runs; choose `collection`, `limit`, and `offset`. The separate
`engagement:read` grant is required. The response contains drafts and correct
answers, but excludes individual responses. Treat it as Organizer-only data.

`engagement.control` requires both `engagement:read` and `engagement:write`.
Read current state and the command schema, prepare the exact action and obtain
explicit operator confirmation. Reuse the returned idempotency key when checking
a receipt; do not invent another key for an uncertain result. Opening, resending,
closing, revealing and sharing on the hall are distinct actions. The hub is still
experimental; keep it disabled until the implementation and release gates in
`ENGAGEMENT-IMPLEMENTATION-PLAN.md` have passed.
