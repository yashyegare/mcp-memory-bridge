# mcp-scratch-client

A from-scratch MCP client (raw JSON-RPC over stdio, no SDK) + a shared
memory MCP server, built as a learning project.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install mcp pytest    # mcp SDK is used ONLY for the test server, not your client
```

## Project layout

```
client/
  raw_client.py     # your hand-rolled MCP client — start here
server/
  test_server.py    # trivial SDK-based server with 2 tools (echo, add)
tests/              # add tests here as you go
```

## Phase 1 — Raw client (current)

1. Open `client/raw_client.py`
2. Fill in the three TODOs: `initialize()`, `list_tools()`, `call_tool()`
3. Reference the spec: https://modelcontextprotocol.io/specification
   (Lifecycle section for handshake, Tools section for the other two)
4. Run it:
   ```bash
   ./venv/bin/python client/raw_client.py
   ```
   Success looks like: server capabilities printed, tool list showing
   `echo` and `add`, and both tool calls returning results.

## Phase 2 — Shared memory server (next)

Not scaffolded yet — build this once Phase 1 works end to end.

- New file: `server/memory_server.py`
- SQLite-backed (WAL mode) with `facts` and `events` tables
- Tools: `memory_get`, `memory_set`, `memory_list`, `memory_delete`
- Point your raw client at it first, then add Claude Desktop as a
  second client via its MCP config and stress-test concurrent writes

## Debugging tips

- If `_recv()` hangs: the server is probably waiting on something you
  didn't send correctly (e.g. sent a request as a notification, or
  vice versa). Check the server's stderr — `test_server.py`'s errors
  land in `self.proc.stderr`.
- Print raw JSON before parsing if messages look malformed.
