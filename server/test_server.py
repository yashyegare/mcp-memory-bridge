"""
Minimal MCP server, built with the official SDK, exposing exactly one
dummy tool. Its only job is to give your hand-rolled client (in
client/raw_client.py) something real to talk to over stdio.

Run it directly to sanity-check it works:
    python server/test_server.py
(it will just sit there waiting for a client on stdin/stdout)

Your own client should spawn this as a subprocess, not run it standalone.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("scratch-test-server")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back whatever text is passed in. Used to verify tools/call works."""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers. A second tool so tools/list has more than one entry."""
    return a + b


if __name__ == "__main__":
    mcp.run(transport="stdio")
