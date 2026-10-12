#!/usr/bin/env python3
"""Read-only smoke test against a live Devo domain, through a real MCP client.

  python scripts/smoke.py                      # spawns `devo-mcp` over stdio
  python scripts/smoke.py --url http://127.0.0.1:8000/mcp   # an HTTP server (MCP_API_KEY from env)

Only read-only tools are called, over short windows with aggregated queries. Prints each tool's
status and the size of its result, not the data itself.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

CALLS = [
    ("devo_check", {}),
    ("devo_tables", {"grep": "collector|win_nxlog"}),
    ("devo_schema", {"table": "siem.logtrust.collector.counter"}),
    ("devo_query", {"linq": "from siem.logtrust.collector.counter group by kind select sum(events) as n",
                    "start": "15m", "limit": 10}),
    ("devo_query", {"linq": "from siem.logtrust.collector.counter group by kind select sum(events) as n",
                    "start": "15m", "limit": 10, "format": "csv"}),
    ("devo_query", {"linq": "from siem.logtrust.collector.counter select collector, kind, events",
                    "start": "5m", "limit": 50, "save": True}),
    ("devo_alerts", {"start": "24h", "by_def": True}),
    ("devo_alert_definitions", {"size": 3}),
    ("devo_boards", {}),
    ("devo_cache", {"action": "status"}),
    ("devo_reference", {}),
    ("devo_reference", {"name": "linq-traps.md", "grep": "weakhas", "limit": 10}),
    ("devo_workspace_list", {}),
    ("devo_query", {"linq": "from siem.logtrust.collector.counter select nosuchfield", "start": "5m"}),  # must fail
]


def http_transport(url: str):
    import httpx2  # the SDK's HTTP client
    from mcp.client.streamable_http import streamable_http_client
    headers = {"Authorization": f"Bearer {os.environ['MCP_API_KEY']}"} if os.environ.get("MCP_API_KEY") else {}
    return streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers, timeout=900))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--show", action="store_true", help="print each result (may contain log data)")
    a = ap.parse_args()
    server = http_transport(a.url) if a.url else StdioServerParameters(
        command=sys.executable, args=["-m", "devo_mcp"], env=dict(os.environ))
    failures = 0
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
        print(f"server: {c.server_info.name if c.server_info else '?'}; {len(tools)} tools")
        for i, (name, args) in enumerate(CALLS):
            expect_error = i == len(CALLS) - 1
            r = await c.call_tool(name, args)
            text = "".join(getattr(x, "text", "") for x in r.content)
            ok = bool(r.is_error) == expect_error
            failures += not ok
            detail = ""
            if not r.is_error:
                try:
                    detail = ", ".join(f"{k}" for k in json.loads(text))
                except Exception:
                    pass
            print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(args)[:70]} -> "
                  f"{'error' if r.is_error else 'ok'} {len(text)} chars {detail[:80]}")
            if a.show or not ok:
                print("   ", text[:1500].replace("\n", "\n    "))
    print("all passed" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
