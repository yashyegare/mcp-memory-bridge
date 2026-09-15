# mcp-scratch-client

A from-scratch MCP client (raw JSON-RPC 2.0 over stdio, no SDK) + a shared
SQLite-backed memory MCP server, built as a learning project. The goal is to
understand the protocol and its concurrency behavior by hand, not by leaning
on libraries that hide the interesting parts.

## Setup (Windows)

```bash
python -m venv venv
venv\Scripts\pip install mcp pytest
```

All commands below use `venv\Scripts\python.exe` explicitly — bare `python`
on PATH may be a different interpreter without the dependencies installed.

## Project layout

```
client/
  raw_client.py       # hand-rolled MCP client: handshake, tools/list, tools/call.
                      #   stdlib only — no mcp SDK import anywhere in it
server/
  test_server.py      # trivial SDK server (echo, add) — Phase 1 target
  memory_server.py    # SQLite-backed shared memory server — Phase 2
tools/
  stress_concurrent.py# multi-client write race harness — Phase 3
tests/                # pytest integration tests (real subprocesses, real wire)
```

## Phase 1 — raw client (done)

```bash
venv\Scripts\python.exe client\raw_client.py
```

Prints the server's capabilities, negotiated protocol version, tool list,
and both tool results. Notable behaviors implemented by hand:

- JSON-RPC framing over stdio (newline-delimited JSON)
- `initialize` → capabilities → `notifications/initialized` (a *notification*:
  no `id`, no reply expected — sending it as a request deadlocks both sides)
- response matching by `id` (notifications and server-initiated requests can
  interleave with responses; "read the next line" is not enough)
- read timeouts via a reader thread + queue, so a hung server can't hang us
- protocol-level errors (JSON-RPC `error` object → `MCPError`) distinguished
  from tool-level errors (successful response with `isError: true`)

## Phase 2 — shared memory server (done)

```bash
# solo smoke test through your own raw client:
venv\Scripts\python.exe -c "from client.raw_client import RawMCPClient; import sys; c = RawMCPClient([sys.executable, 'server/memory_server.py']); c.initialize(); print(c.call_tool('memory_set', {'key': 'demo', 'value': 'hello', 'client_id': 'me'})); print(c.call_tool('memory_get', {'key': 'demo', 'include_events': True})); c.close()"
```

Tools: `memory_set(key, value, client_id)`, `memory_get(key, include_events)`,
`memory_list(prefix)`, `memory_delete(key, client_id)`. Backed by SQLite in
WAL mode; every mutation is attributed (`source_client`) and logged to an
`events` audit table. Missing keys raise a tool-level error with a readable
message (mcp 2.x detail: only `ToolError` messages survive to the client —
arbitrary exceptions get flattened to "Error executing tool <name>").

**SDK version note:** this project uses **mcp 2.x**, where `FastMCP` was
renamed to `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Most
tutorials online still show the 1.x `FastMCP` import, which raises a
`ModuleNotFoundError` on 2.x.

## Phase 3 — concurrency harness (ready)

```bash
venv\Scripts\python.exe tools\stress_concurrent.py --clients 2 --writes 40
```

Spawns independent clients, each with its own server subprocess sharing one
SQLite file, hammering the same key. Every write is verified by an immediate
read; the report shows how often another client's write won and whether any
transport/protocol errors occurred. Inspect the full audit trail with the
command the harness prints at the end.

To add Claude Desktop as a second client, register the server in
`claude_desktop_config.json` with the **absolute** path to
`venv\Scripts\python.exe` and `server\memory_server.py` (relative paths and
PATH-`python` are the classic failure mode there).

## Tests

```bash
venv\Scripts\python.exe -m pytest tests\ -v
```

Integration tests only: each test spawns a real server subprocess and speaks
the real protocol. A silent (hung) server is tested too — the client must
time out, not hang.

## Design decisions

- **SQLite over a graph DB / JSON file.** The point of the project is to
  experience real concurrency semantics: SQLite gives actual transactions,
  row-level busy handling, and WAL mode to reason about. A JSON file has
  none; a graph DB adds a server process and query language this project
  doesn't need.
- **WAL mode + busy_timeout (5s), last-write-wins.** WAL lets readers
  proceed during writes; `busy_timeout` makes near-simultaneous writers
  queue instead of failing with `database is locked`. With writers already
  serialized by the lock, an explicit conflict check would add friction
  without adding information — so we don't reject concurrent writes.
- **Attribution + audit trail instead of conflict rejection.** Every fact
  carries `source_client`; every mutation lands in `events`. An overwrite is
  therefore always detectable and attributable after the fact (`memory_get`
  with `include_events=True`), which is what you actually need to debug a
  shared store. If a future use case needs optimistic concurrency,
  compare-and-swap (`memory_set(..., expected_value=...)`) is the natural
  extension.
- **Known trade-offs.** Single-process-per-client server means one
  connection per client and no cross-machine sharing (Phase 4 option C's
  HTTP transport would change that); values are plain TEXT (no sizes/typing);
  `memory_list`'s prefix match escapes `%`/`_` but remains a plain LIKE scan
  (fine at this scale).

## Debugging tips

- If `_recv()` times out: the server is probably waiting on something you
  didn't send correctly (request sent as a notification, or vice versa), or
  it crashed — check the client's `ConnectionError` message, which includes
  the server's stderr.
- Print raw JSON before parsing if messages look malformed.
- A stray `print()` in server code corrupts the stdio framing — stdout is
  the protocol channel; logs go to stderr.
