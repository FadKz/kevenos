"""Read-only examples for a provisioned KevenOS agent credential."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_client import AgentClient, DEFAULT_URL, token_from_env


async def inspect(base, remote):
    token = token_from_env()
    api = AgentClient(base)
    if not remote:
        await api.request("status", token)
        await api.request("capabilities", token)
        envelope = await api.request("read", token, {"resource": "event.status", "arguments": {}})
        print(json.dumps({key: envelope[key] for key in ("ok", "context", "revision")}, indent=2))
        return

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, follow_redirects=False, trust_env=False,
    ) as client:
        async with streamable_http_client(base.rstrip("/") + "/mcp", http_client=client) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("agent_status")
                print(json.dumps({"tools": [tool.name for tool in tools.tools], "ok": not result.is_error}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-mcp", action="store_true")
    args = parser.parse_args()
    asyncio.run(inspect(os.environ.get("KEVENOS_AGENT_API_URL", DEFAULT_URL), args.remote_mcp))
