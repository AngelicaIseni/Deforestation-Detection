"""Call the server's tools directly, without Claude, to check they work."""

import asyncio
import json

from fastmcp import Client

from server import mcp

CALLS = [
    ("list_alerts", {"limit": 3}),
    ("list_alerts", {"land_status": "mapped by PRODES", "limit": 3}),
    ("get_alert", {"priority_rank": 1}),
    ("get_alert", {"priority_rank": 3}),
    ("get_alert", {"priority_rank": 999}),
    ("whats_here", {"lat": -9.5025, "lon": -63.4512}),
    ("whats_here", {"lat": -9.55, "lon": -63.40}),
    ("whats_here", {"lat": -8.76, "lon": -63.90}),
    ("get_validation_summary", {}),
    ("get_foundation_model_summary", {}),
]


async def main():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print("tools:", [tool.name for tool in tools], "\n")
        for name, arguments in CALLS:
            result = await client.call_tool(name, arguments)
            print(f"--- {name}({arguments})")
            print(json.dumps(result.structured_content, indent=2)[:1500], "\n")


asyncio.run(main())
