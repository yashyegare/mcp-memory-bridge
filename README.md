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

## Phase 3 — two clients, one server (done: live run with Claude Desktop)

**The live experiment:** raw client hammering `live/color` every 300ms
(`tools/live_hammer.py`, each write attributed `raw-hammer`) while Claude
Desktop — registered via `mcpServers` in `claude_desktop_config.json` with
`MCP_CLIENT_ID=claude-desktop` — was asked, mid-stream, to set the same key
to `magenta` and read it back. All from the shared `memory.db`.

What the shared events log showed:

```
#4279 09:21:59.401 raw-hammer      set: live/color=hammer-305
#4281 09:21:59.583 claude-desktop  set: live/color=magenta   <- mid-stream
#4282 09:21:59.707 raw-hammer      set: live/color=hammer-306  <- 124ms later
...
```

- Desktop's write landed **between** two hammer writes and was the live
  value for **124ms** before last-write-wins overwrote it.
- Desktop then read the key back 2.2s later and got `hammer-312` — its own
  write gone. The second client **observed the overwrite happening**, in its
  own conversation, attributed end to end by the events table.
- Zero transport/protocol errors on either side across ~2,500 total writes:
  WAL + 5s busy_timeout + LWW absorbed everything.

Two real bugs surfaced on the way, both general MCP lessons:

1. **Stale spawned servers.** Config changes don't reach servers Desktop
   spawned before the edit — it keeps subprocesses from the old spawn spec.
   Fix: kill the spawned server processes (or fully restart the host) after
   editing `claude_desktop_config.json`. Symptom: connector exists, tools
   list, every call errors for no visible reason.
2. **Cross-thread SQLite crash** (see commit `cb94544`): the SDK dispatches
   tool calls onto different worker threads under load; python's sqlite3
   forbids cross-thread connection sharing by default. Failed only under
   load — early calls landed on the creating thread by luck. Diagnosed via
   the `MCP_DEBUG_LOG` server-side forensic recorder, which captures every
   tool call's args/timing/traceback regardless of what the host does with
   the server's stderr.

**Reproduce the deterministic version** (no Claude Desktop needed):

**Configuration** (per client, via env — Claude Desktop's `mcpServers.env`
works the same way):

- `MCP_MEMORY_DB` / db argv — which SQLite file to share
- `MCP_CLIENT_ID` — attribution for this client's writes and log entries
- `MCP_SQLITE_BUSY_TIMEOUT` — ms a write waits for the lock (default 5000;
  `0` = fail fast, for experiments)
- `MCP_ENABLE_DEMO_TOOLS=1` — adds `lock_hold(seconds)`, a demo tool that
  grabs the write lock and holds it, to make contention deterministic

**The deterministic break** — client A holds the lock while client B writes:

```bash
venv\Scripts\python.exe tools\lock_demo.py          # busy_timeout=0: break
venv\Scripts\python.exe tools\lock_demo.py --queue  # busy_timeout=5000: heal
```

Observed output:

- **busy_timeout=0:** B's write is **rejected in 0.02s** with a tool-level
  error whose message survives to the client: `database is locked
  (busy_timeout=0ms) ... retry with backoff`. The audit trail shows B's
  write never landed. This is fail-fast rejection — the "other" conflict
  policy, switchable with one env var.
- **busy_timeout=5000:** B's write **waits 2.41s**, then commits with full
  attribution (`source: client-B`). Queuing, not erroring — the production
  behavior behind the last-write-wins policy.

**Massive contention:** `MCP_SQLITE_BUSY_TIMEOUT=0 venv\Scripts\python.exe
tools\stress_concurrent.py --clients 4 --writes 50` — 400 operations, 131
reads observed another client's write win, 29 transport-level errors, and
one client wedged mid-run: 13 consecutive read timeouts after its server
stopped responding entirely.

**The wedge is its own lesson — not SQLite's fault.** The wedged server had
its stderr connected to an *undrained pipe*. mcp 2.x logs every tool error
to stderr; ~64KB of log later (a few hundred errors at busy_timeout=0), the
OS pipe buffer fills and the server **blocks on its next stderr write
forever**. Every subsequent call from that client times out; the server
cannot recover. Real MCP hosts (Claude Desktop included) drain stderr
continuously into a log file — which is exactly why this failure almost
never shows up in polished tooling, and exactly why it's worth having seen
once. Your hand-rolled client offers `capture_stderr=False` (drain to
devnull) for stress runs; drain or consume stderr in anything long-lived.

**Adding Claude Desktop as the second client** (already wired in this
checkout): `claude_desktop_config.json` gets

```json
{
  "mcpServers": {
    "memory": {
      "command": "C:\\...\\venv\\Scripts\\python.exe",
      "args": ["C:\\...\\server\\memory_server.py", "C:\\...\\memory.db"],
      "env": { "MCP_CLIENT_ID": "claude-desktop", "MCP_SQLITE_BUSY_TIMEOUT": "5000" }
    }
  }
}
```

Absolute paths are non-negotiable on Windows: relative paths and bare
`python` resolve differently inside Desktop's environment. After a Desktop
restart, the connector UI shows `memory_get/set/list/delete`; ask it to
`memory_set` a key, then watch the attribution and ordering land in your
raw client's `memory_get(key, include_events=True)`.

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
- **What broke in Phase 3, precisely.** With `busy_timeout=0`, contention
  turns into immediate, legible tool-level rejections (the fail-fast policy
  you'd pick if overwrites were unacceptable); with the default 5s, the same
  contention queues and resolves via last-write-wins. The genuinely
  unexpected breakage was operational, not protocol: an undrained stderr
  pipe lets a server's own error logging wedge it permanently under
  sustained errors. Fix: drain stderr (as real MCP hosts do); the client
  exposes `capture_stderr=False` for stress work.

## Debugging tips

- If `_recv()` times out: the server is probably waiting on something you
  didn't send correctly (request sent as a notification, or vice versa), or
  it crashed — check the client's `ConnectionError` message, which includes
  the server's stderr.
- Print raw JSON before parsing if messages look malformed.
- A stray `print()` in server code corrupts the stdio framing — stdout is
  the protocol channel; logs go to stderr.
