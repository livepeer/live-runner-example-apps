"""Prove the remote MCP works: connect over HTTP with a bearer header, list tools,
call a paid ffmpeg tool. Mirrors what `claude mcp add --transport http --header` does.

  uv run test_client.py sk_YOUR_KEY  [input_url]
"""
import asyncio
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    key = sys.argv[1]
    input_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8899/test.mp4"
    url = "http://localhost:9000/mcp"
    headers = {"Authorization": f"Bearer {key}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            print(f"calling ffmpeg_probe(input_url={input_url}) ...")
            res = await session.call_tool("ffmpeg_probe", {"input_url": input_url})
            for c in res.content:
                print("result:", getattr(c, "text", c))


if __name__ == "__main__":
    asyncio.run(main())
