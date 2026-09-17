# mcp-memory-bridge

[![tests](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml)

A from-scratch MCP client (raw JSON-RPC 2.0 over stdio — no SDK) plus a
shared, SQLite-backed memory MCP server that multiple clients hit at once.
Built to understand the MCP protocol and its concurrency behavior by hand,
not through libraries that hide the interesting parts.

## Results at a glance

| Experiment | Outcome |
|---|---|
| Hand-rolled client | Full handshake, `tools/list`, `tools/call` over raw stdio framing — zero SDK imports |
| Live race: Claude Desktop vs raw client | Desktop's `magenta` write landed mid-stream, held the key for **124 ms**, then lost to last-write-wins — and Desktop *read back the value that replaced it* |
| Contention | ~2,500 writes across two independent clients, **0 protocol errors** (WAL + busy_timeout + LWW) |
| Semantic recall | Query *"appearance preference for screens"* — words absent from every stored fact — still retrieved `user/theme` at cosine 0.427 (unrelated fact: 0.036) |
| Warm latency, enforced by tests | `memory_set` ~10 ms (limit 100), `memory_search` ~10 ms (limit 50) |

## Setup (Windows)

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt   # pinned
```

All commands use `venv\Scripts\python.exe` explicitly — bare `python` on
PATH may be a different interpreter without the dependencies.

## Project layout

```
client/
  raw_client.py        # hand-rolled MCP client: handshake, tools/list, tools/call
server/
  test_server.py       # trivial SDK server (echo, add) — Phase 1 target
  memory_server.py     # SQLite-backed shared memory server — Phases 2/3/4
tools/
  stress_concurrent.py # multi-client write race harness
  live_hammer.py       # timed hammer for the live Claude Desktop race
  lock_demo.py         # deterministic lock-contention demo
  inspect_memory.py    # pretty-print the store / history / search log
tests/                 # integration tests: real subprocesses, real wire protocol
```

## How it's built

### Phase 1 — raw MCP client (no SDK)

```bash
venv\Scripts\python.exe client\raw_client.py
```

Implemented by hand against `test_server.py`:

- newline-delimited JSON-RPC framing over stdio
- `initialize` → capabilities → `notifications/initialized` (a *notification*:
  no `id`, no reply expected — sending it as a request deadlocks both sides)
- response matching by `id` on a reader thread + queue (notifications and
  server-initiated requests interleave; "read the next line" is not enough)
- read timeouts, so a hung server can't hang the client
- protocol-level errors (JSON-RPC `error` → `MCPError`) distinguished from
  tool-level errors (successful response with `isError: true`)

### Phase 2 — shared memory server

Tools: `memory_set(key, value, client_id)` · `memory_get(key, include_events)` ·
`memory_list(prefix)` · `memory_delete(key, client_id)`. SQLite in WAL mode;
every mutation is attributed (`source_client`) and appended to an `events`
audit table. Missing keys raise a tool-level error with a readable message
(mcp 2.x detail: only `ToolError` messages survive to the client — arbitrary
exceptions get flattened to "Error executing tool \<name\>").

> **SDK version note:** this project uses **mcp 2.x**, where `FastMCP` was
> renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Most
> tutorials still show the 1.x `FastMCP` import, which fails on 2.x.

Solo smoke test through the raw client:

```bash
venv\Scripts\python.exe -c "from client.raw_client import RawMCPClient; import sys; c=RawMCPClient([sys.executable,'server/memory_server.py']); c.initialize(); print(c.call_tool('memory_set',{'key':'demo','value':'hello','client_id':'me'})); print(c.call_tool('memory_get',{'key':'demo','include_events':True})); c.close()"
```

### Phase 3 — two clients, one server

The live experiment: `tools/live_hammer.py` wrote `live/color` every 300 ms
(attributed `raw-hammer`) while Claude Desktop — registered via `mcpServers`
in `claude_desktop_config.json` with `MCP_CLIENT_ID=claude-desktop` — was
asked mid-stream to set the same key to `magenta`. One shared `memory.db`.

```
#4279 09:21:59.401 raw-hammer      set: live/color=hammer-305
#4281 09:21:59.583 claude-desktop  set: live/color=magenta   <- mid-stream
#4282 09:21:59.707 raw-hammer      set: live/color=hammer-306  <- 124 ms later
```

Desktop's write held as the live value for **124 ms** before last-write-wins
took it back; 2.2 s later Desktop read `hammer-312` — it **observed its own
write being overwritten**, attributed end-to-end by the events table.

Two general MCP lessons surfaced on the way:

1. **Stale spawned servers.** After editing `claude_desktop_config.json`,
   Desktop keeps running servers from the old spawn spec — connector exists,
   tools list, every call errors invisibly. Fix: kill spawned server
   processes or fully restart the host.
2. **Cross-thread SQLite crash** (commit `cb94544`). The SDK dispatches tool
   calls onto different worker threads under load; python's sqlite3 forbids
   cross-thread connection sharing by default. Failed only under load —
   early calls landed on the creating thread by luck. Diagnosed with the
   server-side `MCP_DEBUG_LOG` forensic recorder.

**Deterministic repro** (no Desktop needed) — `lock_demo.py` holds the write
lock in client A while client B writes:

```bash
venv\Scripts\python.exe tools\lock_demo.py          # busy_timeout=0: break
venv\Scripts\python.exe tools\lock_demo.py --queue  # busy_timeout=5000: heal
```

- `busy_timeout=0` — B's write rejected in 0.02 s with a legible tool-level
  error (`database is locked (busy_timeout=0ms) … retry with backoff`); the
  audit trail shows it never landed. Fail-fast: the other conflict policy.
- `busy_timeout=5000` — B's write waits ~2.4 s, then commits with
  attribution. Queuing, not erroring: the production path behind LWW.

Under heavier contention (`MCP_SQLITE_BUSY_TIMEOUT=0 venv\Scripts\python.exe
tools\stress_concurrent.py --clients 4 --writes 50`): 29 transport errors and
one client wedged mid-run — 13 consecutive timeouts after its server stopped
responding. The wedge was operational, not SQLite's fault: the server's
stderr was an **undrained pipe**, mcp 2.x logs every tool error to stderr,
and past ~64 KB the OS pipe buffer fills — the server blocks on its next log
write *forever*. Real hosts drain stderr continuously, which is why polished
tooling never shows this. The client now drains stderr on a daemon thread by
default (bounded tail kept for post-mortems). The same wedge later recurred
via a different cause — sentence-transformers' tqdm progress bars writing to
stderr on every `encode()` — fixed with `show_progress_bar=False`. Same
lesson, second cause.

**Second-client wiring** (already in this checkout):

```json
{ "mcpServers": { "memory": {
    "command": "C:\\...\\venv\\Scripts\\python.exe",
    "args": ["C:\\...\\server\\memory_server.py", "C:\\...\\memory.db"],
    "env": { "MCP_CLIENT_ID": "claude-desktop", "MCP_SQLITE_BUSY_TIMEOUT": "5000" }
} } }
```

Absolute paths are non-negotiable on Windows; after a Desktop restart the
connector panel shows the memory tools.

### Phase 4 — semantic recall + audit CLI

Every `memory_set` embeds the fact (`"key: value"`, all-MiniLM-L6-v2, 384-dim
float32 BLOB in `fact_embeddings`); `memory_search(query, top_k)` embeds the
query and ranks by cosine similarity in plain numpy — no vector DB. Query
words need not appear in any stored fact. Every search is audit-logged
(query, winner, score).

Design notes: the model loads **lazily** on first embedding use (per-process
singleton, single-flight lock) — eager loading would tax every client spawn,
since each MCP client runs its own server subprocess. Once the model is
cached, HF Hub is forced offline so a flaky network can never hang a tool
call. `MCP_EMBEDDINGS=off` gives a lean server with no ML stack;
`MCP_PRELOAD_MODEL=1` pays the load at spawn instead of first call.

`tests\test_perf_benchmarks.py` enforces the latency claims on a ~200-fact
store: warm `memory_set` < 100 ms and `memory_search` < 50 ms (medians; CI
gets 4× headroom).

Phase 4B rode along nearly free: `memory_history(key, include_reads)` exposes
per-key attribution as a tool, and `tools\inspect_memory.py` pretty-prints
the store directly:

```bash
venv\Scripts\python.exe tools\inspect_memory.py --db memory.db    # overview
venv\Scripts\python.exe tools\inspect_memory.py --key live/color  # one key's history
venv\Scripts\python.exe tools\inspect_memory.py --searches        # search log
```

## Configuration

Per client, via env — Claude Desktop's `mcpServers.env` works the same way.

| Variable | Meaning |
|---|---|
| db argv / `MCP_MEMORY_DB` | which SQLite file to share |
| `MCP_CLIENT_ID` | attribution for this client's writes and log entries |
| `MCP_SQLITE_BUSY_TIMEOUT` | ms a write waits for the lock (default 5000; `0` = fail fast) |
| `MCP_EMBEDDINGS` | `off` = lean server, no ML stack |
| `MCP_PRELOAD_MODEL` | `1` = load the embedding model at spawn instead of first use |
| `MCP_ENABLE_DEMO_TOOLS` | `1` = add `lock_hold(seconds)` demo tool for contention demos |
| `MCP_DEBUG_LOG` | path; server-side forensic record of every tool call (args, timing, tracebacks) |

## Tests

```bash
venv\Scripts\python.exe -m pytest tests\ -v
```

21 integration tests; every test spawns a real server subprocess and speaks
the real wire protocol — including one where the server hangs silently and
the client must time out, not hang. Non-semantic tests run with
`MCP_EMBEDDINGS=off` so the suite doesn't pay the model load per test.

## Design decisions

- **SQLite, not a graph DB or JSON file.** The point is experiencing real
  concurrency semantics: transactions, busy handling, WAL. A JSON file has
  none of that; a graph DB adds infrastructure this project doesn't need.
- **Last-write-wins, audit trail as the safety net.** Writes are attributed
  and every mutation lands in `events`, so an overwrite is always detectable
  and attributable after the fact — what you actually need to debug a shared
  store. If optimistic concurrency were needed, compare-and-swap
  (`memory_set(..., expected_value=...)`) is the natural extension.
- **What WAL does and does not buy here.** The concurrency that matters is
  *across* processes (each client spawns its own server), and there WAL
  genuinely lets a reader proceed during another process's write
  transaction. *Within* one process, `_SERVER_LOCK` deliberately serializes
  every tool call — reads included — so WAL's reader/writer parallelism
  never applies in-process. Conscious trade: one connection plus a coarse
  lock is simpler and always correct; claiming WAL for in-process reads
  would be overclaiming.
- **Semantic search without a vector DB.** Embeddings in a plain SQLite
  table, brute-force cosine in numpy — sub-10 ms at hundreds to low
  thousands of facts, zero extra infrastructure. An ANN index (FAISS/
  hnswlib) is the upgrade path if the store grows, not a day-one need.
- **Known trade-offs.** One server per client means no cross-machine sharing
  (the HTTP/SSE transport option would change that); values are plain TEXT;
  `memory_list` is a prefix `LIKE` scan (fine at this scale).
- **Concurrency Control (CAS & Create-Only):**  
  To safely handle multiple agents interacting with the same memory instance, we rely on optimistic concurrency control. Clients can use Compare-And-Swap (CAS) by passing an `expected_value` with their write requests; the update only succeeds if the underlying data hasn't been altered by another process in the meantime. Alternatively, clients can pass a `require_absent` flag to enforce create-only semantics, ensuring a key is safely initialized without overwriting existing data. Both mechanisms prevent race conditions and lost updates without the need for complex external locking.

## Debugging tips

- `_recv()` timing out usually means the server is waiting on a message you
  sent wrong (request vs notification), or it crashed — the client's
  `ConnectionError` includes the server's stderr.
- A stray `print()` in server code corrupts stdio framing: stdout is the
  protocol channel; logs go to stderr.

## License

MIT — see [LICENSE](LICENSE).
