# mcp-scratch-client

[![tests](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml)

A from-scratch MCP client (raw JSON-RPC 2.0 over stdio, no SDK) + a shared
SQLite-backed memory MCP server, built as a learning project. The goal is to
understand the protocol and its concurrency behavior by hand, not by leaning
on libraries that hide the interesting parts.

## Setup (Windows)

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt   # pinned; includes sentence-transformers (Phase 4A)
```

Semantic recall is optional at runtime: `MCP_EMBEDDINGS=off` runs the server
without the ML stack entirely.

All commands below use `venv\Scripts\python.exe` explicitly — bare `python`
on PATH may be a different interpreter without the dependencies installed.

## Project layout

```
client/
  raw_client.py       # hand-rolled MCP client: handshake, tools/list, tools/call.
                      #   stdlib only — no mcp SDK import anywhere in it
server/
  test_server.py      # trivial SDK server (echo, add) — Phase 1 target
  memory_server.py    # SQLite-backed shared memory server — Phases 2/3/4
tools/
  stress_concurrent.py # multi-client write race harness — Phase 3
  live_hammer.py       # timed hammer for the live Claude Desktop race
  lock_demo.py         # deterministic lock-contention demo
  inspect_memory.py    # pretty-print the store/history/search log — Phase 4B
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
once. The hand-rolled client now **drains stderr continuously on a
daemon thread** (keeping a bounded tail for post-mortems) — the same thing
real hosts do. `capture_stderr=False` (straight to devnull) remains
available for stress runs as a zero-copy option.

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

## Phase 4 — semantic recall + audit CLI (done)

The memory server now finds facts by **meaning**, not just exact keys.
Every `memory_set` embeds the fact (all-MiniLM-L6-v2, 384-dim, stored as a
BLOB in `fact_embeddings`); `memory_search(query, top_k)` embeds the query
and returns the best facts by cosine similarity — plain numpy, no vector
DB. Live proof, through the raw client:

```
query: "appearance preference for screens"  (those words appear in NO stored fact)
  user/theme = the user prefers dark mode interfaces  (cosine 0.427)
  pet/name  = Fluffy is a tabby cat                  (cosine 0.036)
```

Design notes: the model loads **lazily** on first use (per-process singleton,
single-flight under the `_model_lock`) — loading at startup would tax every
client spawn, since each MCP client runs its own server subprocess. Once the
model is in the local HF cache, the server forces `HF_HUB_OFFLINE=1`, so a
flaky network can never hang a tool call; on a fresh machine it allows the
one-time download instead. Set `MCP_EMBEDDINGS=off` for a lean server with
no ML dependencies; set `MCP_PRELOAD_MODEL=1` if your host prefers paying
the load at spawn instead of on first call. Every search is audit-logged
(query, winner, score). The latency claims are enforced, not aspirational:
`tests\test_perf_benchmarks.py` asserts warm `memory_set` < 100ms and warm
`memory_search` < 50ms (medians over a ~200-fact store; CI gets 4× headroom).

Phase 4B rode along nearly free (the events table already existed):
`memory_history(key, include_reads)` exposes per-key attribution history as
a tool, and `tools\inspect_memory.py` pretty-prints the store directly:

```bash
venv\Scripts\python.exe tools\inspect_memory.py --db memory.db            # overview
venv\Scripts\python.exe tools\inspect_memory.py --key live/color          # one key's history
venv\Scripts\python.exe tools\inspect_memory.py --searches                # semantic search log
```

## Tests

```bash
venv\Scripts\python.exe -m pytest tests\ -v
```

19 integration tests across four phases. Non-semantic tests run with
`MCP_EMBEDDINGS=off` so the suite doesn't pay the model load per test; one
combined lifecycle test covers the full semantic path (meaning-based hit,
audit log, delete cascade).

Integration tests only: each test spawns a real server subprocess and speaks
the real protocol. A silent (hung) server is tested too — the client must
time out, not hang.

## Design decisions

- **SQLite over a graph DB / JSON file.** The point of the project is to
  experience real concurrency semantics: SQLite gives actual transactions,
  row-level busy handling, and WAL mode to reason about. A JSON file has
  none; a graph DB adds a server process and query language this project
  doesn't need.
- **WAL mode + busy_timeout (5s), last-write-wins — and what WAL does *not*
  buy here.** `busy_timeout` makes near-simultaneous writers queue instead
  of failing with `database is locked`; with writers already serialized, an
  explicit conflict check would add friction without adding information, so
  we don't reject concurrent writes. But an honest caveat: each MCP client
  spawns its own server subprocess, so the concurrency that matters is
  *across* processes — and there WAL genuinely lets a reader proceed during
  another process's open write transaction (the `inspect_memory.py` CLI
  reading while a hammer writes is exactly this). *Within* one process,
  `_SERVER_LOCK` deliberately serializes every tool call, reads included,
  so WAL's reader/writer parallelism never applies in-process. That's a
  conscious trade, not an oversight: one shared connection plus a coarse
  lock is dramatically simpler and always correct; a connection pool or
  read/write lock split would buy concurrency this workload doesn't need.
  Claiming WAL for in-process reads would be overclaiming.
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
- **Semantic recall without a vector DB.** Embeddings live in a plain
  SQLite table and search is a brute-force cosine scan in numpy — at
  hundreds-to-low-thousands of facts that is sub-10ms and needs zero extra
  infrastructure. The embedding text is `"key: value"` so key names
  contribute signal. A real ANN index (FAISS/hnswlib) is the upgrade path
  if the store grows, not a day-one need.
- **Lazy model loading, offline by default.** Server startup stays instant
  (~1s) regardless of ML stack; the ~25s model load happens once, on first
  embedding use, per server process. HF Hub is put in offline mode once the
  model is cached so tool calls can never hang on a network check.
- **What broke in Phase 3, precisely.** With `busy_timeout=0`, contention
  turns into immediate, legible tool-level rejections (the fail-fast policy
  you'd pick if overwrites were unacceptable); with the default 5s, the same
  contention queues and resolves via last-write-wins. The genuinely
  unexpected breakage was operational, not protocol: an undrained stderr
  pipe lets a server's own error logging wedge it permanently under
  sustained errors. Fix: drain stderr (as real MCP hosts do), which the
  client now does by default. This failure mode came back a *second*
  time from a different direction: sentence-transformers' tqdm progress
  bars write to stderr on every `encode()` call, and ~200 warm-up writes
  filled the pipe buffer with progress-bar refreshes — wedging a server
  whose stderr was captured but not drained. Same lesson, second cause:
  the server now passes `show_progress_bar=False`, and the client drains.

## License

MIT — see [LICENSE](LICENSE).

## Debugging tips

- If `_recv()` times out: the server is probably waiting on something you
  didn't send correctly (request sent as a notification, or vice versa), or
  it crashed — check the client's `ConnectionError` message, which includes
  the server's stderr.
- Print raw JSON before parsing if messages look malformed.
- A stray `print()` in server code corrupts the stdio framing — stdout is
  the protocol channel; logs go to stderr.
