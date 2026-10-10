"""Probe chrome-devtools-mcp against your running Chrome:  PYTHONPATH=src .venv/bin/python scripts/devtools_probe.py
Prints the server's tool names and whether it could attach (counts only: never tab titles or URLs)."""
import asyncio, os, sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ARGS = ["-y", "chrome-devtools-mcp@latest", "--autoConnect", "--no-usage-statistics", "--no-performance-crux"]


async def main() -> None:
    env = {**os.environ, "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1", "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"}
    params = StdioServerParameters(command="npx", args=ARGS, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 120)
            tools = (await session.list_tools()).tools
            print(f"server up: {len(tools)} tools:", ", ".join(t.name for t in tools))
            try:
                res = await asyncio.wait_for(session.call_tool("list_pages", {}), 90)
            except asyncio.TimeoutError:
                print("list_pages: timed out (is Chrome showing an 'Allow' dialog? click Allow, then run this again)")
                return
            text = "".join(getattr(c, "text", "") for c in res.content)
            lines = [l for l in text.splitlines() if l.strip()]
            print("list_pages isError =", res.is_error, "| lines returned:", len(lines))
            if res.is_error:
                print("error text:", text[:400])

asyncio.run(main())
