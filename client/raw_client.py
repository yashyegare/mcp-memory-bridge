"""
A from-scratch MCP client. No mcp SDK used here on purpose.

What's done for you: spawning the server subprocess and reading/writing
newline-delimited JSON over its stdin/stdout (MCP's stdio transport
framing). That's just plumbing, not the learning objective.

What you need to implement (marked TODO below): the actual JSON-RPC 2.0
message shapes and the MCP handshake sequence. This is the part that
will teach you the protocol.

Reference while you build: https://modelcontextprotocol.io/specification
Look specifically at:
  - "Lifecycle" section for the initialize/initialized handshake
  - "Tools" section for tools/list and tools/call message shapes
"""

import json
import subprocess
import itertools


class RawMCPClient:
    def __init__(self, server_command: list[str]):
        self.proc = subprocess.Popen(
            server_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._id_counter = itertools.count(1)

    def _next_id(self) -> int:
        return next(self._id_counter)

    def _send(self, message: dict) -> None:
        """Write one JSON-RPC message to the server's stdin, newline-delimited."""
        line = json.dumps(message)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        """Read one JSON-RPC message from the server's stdout."""
        line = self.proc.stdout.readline()
        if not line:
            stderr_output = self.proc.stderr.read()
            raise ConnectionError(f"Server closed stdout. Stderr: {stderr_output}")
        return json.loads(line)

    def initialize(self) -> dict:
        """
        TODO: Implement the MCP handshake.

        Steps:
        1. Send a JSON-RPC request with method "initialize". It needs
           an "id", and params including protocolVersion, capabilities,
           and clientInfo (name + version). Check the spec for the
           exact params shape.
        2. Read the response via self._recv() and confirm it doesn't
           contain an "error" key.
        3. Send a JSON-RPC *notification* (no "id" field!) with method
           "notifications/initialized" and empty params. This tells
           the server you're ready.
        4. Return the server's capabilities from step 2's response so
           the caller can inspect them.
        """
        raise NotImplementedError("Implement the initialize handshake")

    def list_tools(self) -> list[dict]:
        """
        TODO: Implement tools/list.

        Send a request with method "tools/list" (no params needed).
        The response's result will contain a "tools" array — return it.
        """
        raise NotImplementedError("Implement tools/list")

    def call_tool(self, name: str, arguments: dict) -> dict:
        """
        TODO: Implement tools/call.

        Send a request with method "tools/call" and params containing
        "name" and "arguments". Return the result field of the response.
        Watch for the "isError" field in the result — that's how MCP
        signals a tool-level error (as opposed to a protocol-level one).
        """
        raise NotImplementedError("Implement tools/call")

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


if __name__ == "__main__":
    # Quick manual smoke test once you've filled in the TODOs above.
    client = RawMCPClient(["python", "server/test_server.py"])
    try:
        caps = client.initialize()
        print("Server capabilities:", caps)

        tools = client.list_tools()
        print("Available tools:", [t["name"] for t in tools])

        result = client.call_tool("echo", {"text": "hello from raw client"})
        print("Tool result:", result)

        result = client.call_tool("add", {"a": 2, "b": 3})
        print("Tool result:", result)
    finally:
        client.close()
