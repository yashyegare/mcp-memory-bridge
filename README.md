# mcp-memory-bridge

[![tests](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml)

**The problem:** MCP clients (Claude Desktop, Cursor, custom agents) each
spawn their own tools — nothing shares memory between them. This project
builds a **shared-memory MCP server**: one SQLite store that multiple
independent clients read and write concurrently, with attribution and an
audit trail for every change — plus a **from-scratch MCP client** (raw
JSON-RPC 2.0 over stdio, no SDK) used to exercise it.

```
 ┌──────────────┐   ┌──────────────┐   ┌───────────────┐
 │ Claude       │   │ Cursor /     │   │ raw_client.py │
 │ Desktop      │   │ any MCP host │   │ (hand-rolled, │
 │ (MCP client) │   │ (MCP client) │   │  no SDK)      │
 └──────┬───────┘   └──────┬───────┘   └──────┬────────┘
        │  stdio           │  stdio           │  stdio
        ▼                  ▼                  ▼
 ┌─────────────────────────────────────────────────────┐
 │ memory_server.py — one MCP server per client,       │
 │ all pointing at the same store                      │
 └──────────────────────────┬──────────────────────────┘
                            ▼
              ┌──────────────────────────┐
              │ SQLite (WAL): facts +    │
              │ events audit trail +     │
              │ fact_embeddings          │
              └──────────────────────────┘
```

Built to understand the protocol and its concurrency behavior by hand, not
through libraries that hide the interesting parts.

## Results at a glance

| Experiment | Outcome |
|---|---|
| Hand-rolled client | Full handshake, `tools/list`, `tools/call` over raw stdio framing — zero SDK imports |
| Live race: Claude Desktop vs raw client | Desktop's `magenta` write landed mid-stream, held the key for **124 ms**, then lost to last-write-wins — and Desktop *read back the value that replaced it* |
| Contention | ~2,500 writes across two independent clients, **0 protocol errors** (WAL + busy_timeout + LWW) |
| Semantic recall | Query *"appearance preference for screens"* — words absent from every stored fact — still retrieved `user/theme` at cosine 0.427 (unrelated fact: 0.036) |
| Warm latency, enforced by tests | `memory_set` ~10 ms (limit 100), `memory_search` ~10 ms (limit 50) |

## Setup

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt        # Windows
source venv/bin/activate && pip install -r requirements.txt   # Linux/macOS
```

All commands below use `venv\Scripts\python.exe` (Windows); on Linux/macOS
use `venv/bin/python` or activate the venv.

**Docker (lean mode).** The server ships as an image with the ML stack
excluded (`MCP_EMBEDDINGS=off` baked in) — useful when a host prefers
launching a container as its stdio subprocess:

```json
{ "command": "docker",
  "args": ["run", "--rm", "-i", "-v", "C:\\...\\memory.db:/data/memory.db",
            "mcp-memory-bridge", "python", "server/memory_server.py", "/data/memory.db"] }
```

CI builds the image and smoke-tests the handshake through it on every push.

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
docs/NOTES.md          # the debugging stories: what broke and why
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

Tools: `memory_set` (with optional CAS) · `memory_get` · `memory_list` ·
`memory_delete` · `memory_search` · `memory_history`. SQLite in WAL mode;
every mutation is attributed (`source_client`) and appended to an `events`
audit table.

> **SDK version note:** this project uses **mcp 2.x**, where `FastMCP` was
> renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Most
> tutorials still show the 1.x `FastMCP` import, which fails on 2.x.

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

Three things broke along the way — stale spawned servers, a cross-thread
SQLite crash, and an undrained stderr wedge (twice). Each is a general
engineering lesson; the full stories are in [docs/NOTES.md](docs/NOTES.md).

**Deterministic repro** (no Desktop needed) — `lock_demo.py` holds the write
lock in client A while client B writes:

```bash
venv\Scripts\python.exe tools\lock_demo.py          # busy_timeout=0: break
venv\Scripts\python.exe tools\lock_demo.py --queue  # busy_timeout=5000: heal
```

- `busy_timeout=0` — B's write rejected in 0.02 s with a legible tool-level
  error; the audit trail shows it never landed. Fail-fast: the other
  conflict policy.
- `busy_timeout=5000` — B's write waits ~2.4 s, then commits with
  attribution. Queuing, not erroring: the production path behind LWW.

### Phase 4 — semantic recall + audit CLI

Every `memory_set` embeds the fact (`"key: value"`, all-MiniLM-L6-v2, 384-dim
float32 BLOB in `fact_embeddings`); `memory_search(query, top_k)` embeds the
query and ranks by cosine similarity in plain numpy — no vector DB. Query
words need not appear in any stored fact. Every search is audit-logged.

Design notes: the model loads **lazily** on first embedding use (per-process
singleton, single-flight lock) — eager loading would tax every client spawn,
since each MCP client runs its own server subprocess. Once the model is
cached, HF Hub is forced offline so a flaky network can never hang a tool
call. `MCP_EMBEDDINGS=off` gives a lean server with no ML stack;
`MCP_PRELOAD_MODEL=1` pays the load at spawn instead of first call.

`tests/test_perf_benchmarks.py` enforces the latency claims on a ~200-fact
store: warm `memory_set` < 100 ms and `memory_search` < 50 ms (medians; CI
gets 4× headroom).

The audit trail is also a tool: `memory_history(key, include_reads)` returns
per-key attribution history (writes, failed CAS attempts, optionally reads
and search hits), and `tools/inspect_memory.py` pretty-prints the store:

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
| `MCP_LOG_READS` | `off` = `memory_get` becomes truly read-only (see design notes) |
| `MCP_MAX_KEY_LEN` / `MCP_MAX_VALUE_LEN` | input caps (defaults 256 / 16384 chars) |
| `MCP_ENABLE_DEMO_TOOLS` | `1` = add `lock_hold(seconds)` demo tool for contention demos |
| `MCP_DEBUG_LOG` | path; server-side forensic record of every tool call |

## Tests

```bash
venv\Scripts\python.exe -m pytest tests\ -v    # Windows
venv/bin/python -m pytest tests/ -v            # Linux/macOS
```

30 integration tests (28 storage/protocol + 2 that load the embedding
model: the semantic lifecycle and the latency benchmark); every test
spawns a real server subprocess and speaks the real wire protocol —
including one where the server hangs silently and the client must time
out, not hang. Non-semantic tests run with `MCP_EMBEDDINGS=off` so the
suite doesn't pay the model load per test.

## Design decisions

- **SQLite, not a graph DB or JSON file.** The point is experiencing real
  concurrency semantics: transactions, busy handling, WAL. A JSON file has
  none of that; a graph DB adds infrastructure this project doesn't need.
- **Last-write-wins, audit trail as the safety net.** Writes are attributed
  and every mutation lands in `events`, so an overwrite is always detectable
  and attributable after the fact. Callers who want rejection opt in per
  call: compare-and-swap (`expected_value=...`) or create-only
  (`require_absent=True`), each a single atomic SQL statement.
- **What WAL does and does not buy here.** The concurrency that matters is
  *across* processes, and there WAL genuinely lets a reader proceed during
  another process's write transaction. Two honest caveats: (1) *within* one
  process, `_SERVER_LOCK` serializes every tool call — WAL's reader/writer
  parallelism never applies in-process; (2) by default `memory_get` logs an
  event row, so **reads are writes** — they take the write lock and can
  queue behind another client. `MCP_LOG_READS=off` restores a genuinely
  read-only get at the cost of read attribution.
- **Attribution is trust-based, by design.** `client_id` is a plain tool
  argument — any caller can claim any identity. That's acceptable because
  the store's threat model is *debugging*, not security: attribution answers
  "who wrote this," not "is this allowed." Real auth would need the
  transport layer to authenticate clients before a word of the protocol is
  spoken.
- **Input caps.** Keys and values are length-capped (defaults 256 / 16384)
  because a shared TEXT store with no limits is one `memory_set` away from
  a bloated database.
- **Semantic search without a vector DB.** Embeddings in a plain SQLite
  table, brute-force cosine in numpy — sub-10 ms at hundreds to low
  thousands of facts, zero extra infrastructure. An ANN index is the
  upgrade path if the store grows, not a day-one need.
- **Known trade-offs.** One server per client means no cross-machine
  sharing (the HTTP/SSE transport option would change that); `memory_list`
  is a prefix `LIKE` scan (fine at this scale).

## Debugging

The debugging stories — stale spawned servers, the cross-thread SQLite
crash, the stderr wedge (twice), and the import-order env footgun — live in
[docs/NOTES.md](docs/NOTES.md), each with symptom, diagnosis, fix, and what
it generalizes to.

Quick tips: a `_recv()` timeout usually means the server is waiting on a
message you sent wrong, or it crashed (the client's `ConnectionError`
includes the drained stderr tail). A stray `print()` in server code
corrupts stdio framing — stdout is the protocol channel; logs go to stderr.

## License

MIT — see [LICENSE](LICENSE).
