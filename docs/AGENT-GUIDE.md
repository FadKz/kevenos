# KevenOS agent operator guide

This package exposes an optional MCP and HTTP API for an event operator's
agent. It contains no production credentials, event records, attendee data, or
deployment addresses.

## Connect

- Local MCP: run `python agent_mcp.py --transport stdio` from this directory.
- Remote MCP: configure the application with `KEVENOS_AGENT_TOKENS` and connect to
  `${KEVENOS_PUBLIC_URL}/mcp` using the corresponding bearer token.
- HTTP API: use `${KEVENOS_PUBLIC_URL}/api/agent/v1` with the same scoped token.

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
