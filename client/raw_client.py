"""
A from-scratch MCP client. No mcp SDK used here on purpose.

Everything below speaks raw JSON-RPC 2.0 over stdio (newline-delimited
JSON) by hand:

  - initialize()       the MCP lifecycle handshake:
                         request "initialize" (with id) ->
                         server reply carries protocolVersion/capabilities ->
                         we send the "notifications/initialized"
                         *notification* (no id -- a notification is fire-
                         and-forget, the server must not reply to it)
  - list_tools()       "tools/list", including cursor pagination if the
                         server pages its tool list
  - call_tool()        "tools/call"; protocol-level failures come back as
                         a JSON-RPC "error" object, tool-level failures
                         come back as a *successful* response whose result
                         has isError=true -- know which is which

Plumbing that libraries normally hide, which we do here by hand:
  - message framing: one JSON object per line over the server's stdin/stdout
  - matching responses to requests by "id" (responses can be interleaved
    with notifications or server-initiated requests, so "read the next
    line" is NOT enough)
  - timeouts on reads so a hung server doesn't hang us forever

Reference: https://modelcontextprotocol.io/specification
  (Lifecycle section for the handshake, Tools section for the other two)
"""

import collections
import itertools
import json
import os
import queue
import subprocess
import sys
import threading


class MCPError(Exception):
    """A JSON-RPC error response from the server (protocol-level error)."""

    def __init__(self, code, message, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}" + (f" (data: {data!r})" if data is not None else ""))


class RawMCPClient:
    def __init__(
        self,
        server_command: list[str],
        protocol_version: str = "2025-11-25",
        # 45s: the first semantic-search-enabled tool call loads an ~80MB
        # embedding model (~25s cold). Hung-server protection is still
        # available via an explicit shorter timeout (see the tests).
        default_timeout: float = 45.0,
        env: dict[str, str] | None = None,
        capture_stderr: bool = True,
    ):
        self.protocol_version = protocol_version
        self.default_timeout = default_timeout
        # env entries are overlaid on top of the parent environment (so PATH
        # etc. survive) -- this is how per-client server config like
        # MCP_CLIENT_ID gets through, mirroring Claude Desktop's "env" key.
        child_env = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(
            server_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr=PIPE gets drained continuously by a dedicated thread
            # (see _stderr_tail below) -- the Phase 3 lesson: an undrained
            # stderr PIPE fills its ~64KB OS buffer and BLOCKS the server
            # the moment it logs more than that (a tool erroring on every
            # call, or tqdm progress bars). capture_stderr=False => DEVNULL
            # for stress runs that want zero copy overhead at all.
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered
            env=child_env,
        )
        self._id_counter = itertools.count(1)
        # A server can write notifications (or its own requests) to stdout
        # between our responses, so reads happen on a dedicated thread and
        # everything lands on one queue; _recv() filters for what it needs.
        self._incoming: queue.Queue = queue.Queue()
        self._notifications: list[dict] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Real hosts drain the server's stderr continuously. Capturing
        # stderr without draining is exactly how the Phase 3 wedge happens:
        # once ~64KB of un-read output fills the OS pipe buffer, the server
        # blocks on its next log write and every call times out. Keep a
        # bounded tail (post-mortem value in close()/error paths) without
        # unbounded memory or a blocked server.
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
        if self.proc.stderr is not None:
            threading.Thread(target=self._drain_stderr, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Plumbing                                                           #
    # ------------------------------------------------------------------ #

    def _next_id(self) -> int:
        return next(self._id_counter)

    def _send(self, message: dict) -> None:
        """Write one JSON-RPC message to the server's stdin, newline-delimited."""
        line = json.dumps(message)
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ConnectionError(f"Server stdin closed: {exc}\nStderr: {self._read_stderr()}") from exc

    def _drain_stderr(self) -> None:
        """Drain the server's stderr forever so it can never block on a log
        write; retain the most recent lines for post-mortems."""
        try:
            for line in self.proc.stderr:
                self._stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            pass  # stderr closed during teardown

    def _read_stderr(self) -> str:
        """Recent stderr output (continuously drained, so this never blocks
        and is safe to call at any time, process alive or dead)."""
        if self.proc.stderr is None:
            return "<stderr not captured>"
        if self._stderr_tail:
            return "\n".join(self._stderr_tail)
        return "(stderr empty)"

    def _read_loop(self) -> None:
        """Reader thread: push every stdout line onto the queue; push None on EOF."""
        try:
            for line in self.proc.stdout:
                try:
                    self._incoming.put(json.loads(line))
                except json.JSONDecodeError as exc:
                    # Non-JSON junk on stdout (a stray print() in the server
                    # is the classic cause) would corrupt the framing.
                    self._incoming.put({"_framing_error": f"unparseable line: {line!r} ({exc})"})
        finally:
            self._incoming.put(None)  # EOF sentinel

    def _recv(self, timeout: float) -> dict:
        """Read one raw inbound message, with a timeout so a hung server
        can't hang us. Raises ConnectionError on EOF/timeout."""
        try:
            msg = self._incoming.get(timeout=timeout)
        except queue.Empty:
            raise ConnectionError(f"No message from server within {timeout}s") from None
        if msg is None:
            raise ConnectionError(f"Server closed stdout. Stderr: {self._read_stderr()}")
        if "_framing_error" in msg:
            raise ConnectionError(f"Bad framing from server: {msg['_framing_error']}")
        return msg

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Send a JSON-RPC request and return the matching result.

        Skips over notifications and unrelated traffic until the response
        with our id arrives; answers server-initiated requests with a
        method-not-found error so the server never blocks on us.
        """
        request_id = self._next_id()
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline_timeout = self.default_timeout if timeout is None else timeout
        while True:
            msg = self._recv(deadline_timeout)
            if "id" not in msg:
                # A notification (no id, no reply expected) -- e.g.
                # notifications/tools/list_changed. Stash and keep waiting.
                self._notifications.append(msg)
                continue
            if msg.get("id") != request_id:
                if "method" in msg:
                    # Server-initiated *request* (has both method and id): we
                    # must reply something or the server may block on it.
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "Method not found (raw client)"}})
                continue  # response to some older id: shouldn't happen; keep waiting
            if "error" in msg:
                err = msg["error"]
                raise MCPError(err.get("code"), err.get("message"), err.get("data"))
            return msg.get("result", {})

    # ------------------------------------------------------------------ #
    # MCP protocol methods                                               #
    # ------------------------------------------------------------------ #

    def initialize(self) -> dict:
        """MCP handshake: initialize request -> capabilities -> initialized
        notification. Returns the server's InitializeResult."""
        result = self._request(
            "initialize",
            params={
                "protocolVersion": self.protocol_version,
                # What *we* support; the server picks from its own list.
                "capabilities": {},
                "clientInfo": {"name": "raw-scratch-client", "version": "0.1.0"},
            },
            timeout=15.0,  # first request pays server startup/import cost
        )
        server_version = result.get("protocolVersion")
        if server_version != self.protocol_version:
            # Spec: server replies with a version it supports; we offered
            # only one, so any other value is a negotiation surprise.
            print(f"warning: server negotiated protocolVersion {server_version!r} "
                  f"(we offered {self.protocol_version!r})", file=sys.stderr)
        if "serverInfo" not in result:
            raise MCPError(-32602, "initialize result missing serverInfo", result)
        # Notification: NO "id" field. The server must not reply to this;
        # sending it as a request would leave both sides waiting.
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    def list_tools(self) -> list[dict]:
        """tools/list. Follows the server's cursor pagination if present."""
        tools: list[dict] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params=params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        """tools/call. Returns the raw CallToolResult.

        Two very different failure shapes (don't conflate them):
          - protocol-level: _request raises MCPError from the JSON-RPC
            "error" object (e.g. unknown tool name)
          - tool-level: a *successful* response whose result has
            isError=True; the human-readable failure lives in content
        """
        return self._request("tools/call", params={"name": name, "arguments": arguments})

    # ------------------------------------------------------------------ #
    # Teardown                                                           #
    # ------------------------------------------------------------------ #

    def close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


# ---------------------------------------------------------------------- #
# Smoke test                                                             #
# ---------------------------------------------------------------------- #

def _text_of(result: dict) -> str:
    """Concatenate the text blocks of a CallToolResult's content."""
    return "\n".join(
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    )


if __name__ == "__main__":
    # sys.executable, not "python": the venv that has the mcp SDK installed
    # is the interpreter running THIS file; bare `python` on PATH may be a
    # different install without mcp.
    client = RawMCPClient([sys.executable, "server/test_server.py"])
    try:
        caps = client.initialize()
        print("Server capabilities:")
        print(json.dumps(caps, indent=2))
        print()

        tools = client.list_tools()
        print("Available tools:", [t["name"] for t in tools])
        print()

        result = client.call_tool("echo", {"text": "hello from raw client"})
        print("echo ->", _text_of(result))

        result = client.call_tool("add", {"a": 2, "b": 3})
        print("add ->", _text_of(result))
        print()

        # Error-shape demo: calling an unknown tool can fail at EITHER
        # level, and both are spec-legal -- servers choose. Protocol-level
        # = JSON-RPC error object (we raise MCPError); tool-level = success
        # response with isError=true. This SDK picks tool-level.
        try:
            result = client.call_tool("does_not_exist", {})
        except MCPError as exc:
            print(f"unknown tool -> protocol-level error: {exc}")
        else:
            assert result.get("isError") is True, f"unexpected success: {result}"
            print(f"unknown tool -> tool-level error (isError=true): {_text_of(result)!r}")
    finally:
        client.close()
