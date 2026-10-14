"""Call the server's tools directly, without Claude, to check they work."""

import asyncio

from fastmcp import Client

from server import mcp


async def main():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print("tools:", [tool.name for tool in tools])
        result = await client.call_tool("get_run_info", {})
        print(result.data)


asyncio.run(main())