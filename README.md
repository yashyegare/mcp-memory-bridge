# mcp-memory-bridge

[![tests](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml)

MCP clients running separate tool processes need a common store to share
facts. This project provides a SQLite-backed memory server that multiple
clients can read and write, plus a hand-written JSON-RPC client for exercising
the protocol over stdio and HTTP.

Current features include

- Shared facts with source attribution, last-write-wins, and atomic CAS.
- Versioned audit events and replay checks for facts and embeddings.
- HMAC authenticated checkpoints, online full backups, and incremental restore.
- HTTP client identities, token hashes, live revocation, and read/write scopes.
- Authenticated health and metrics endpoints, redacted debug logs, and a
  non-root Docker image.
- Optional semantic search with a local embedding model.

**Two transport modes share one store.**

```
stdio mode — each client spawns its own server subprocess:

 ┌──────────────┐   ┌──────────────┐   ┌───────────────┐
 │ Claude       │   │ Cursor /     │   │ raw_client.py │
 │ Desktop      │   │ any MCP host │   │ (hand-rolled, │
 │ (MCP client) │   │ (MCP client) │   │  no SDK)      │
 └──────┬───────┘   └──────┬───────┘   └──────┬────────┘
        │  stdio           │  stdio           │  stdio
        ▼                  ▼                  ▼
 ┌─────────────────────────────────────────────────────┐
 │ memory_server.py — one subprocess per client,       │
 │ all pointing at the same store                      │
 └──────────────────────────┬──────────────────────────┘
                            ▼
              ┌──────────────────────────┐
              │ SQLite (WAL): facts +    │
              │ events audit trail +     │
              │ fact_embeddings          │
              └──────────────────────────┘

```

HTTP mode serves multiple client sessions from one running server.

```text
 MCP clients ──HTTP──► memory_server.py ──► shared SQLite database
                        bearer authentication
                        localhost by default
```

Stdio clients each launch a server process and share the same database path.
HTTP clients connect to a single running server. A TLS proxy can expose the HTTP
transport for remote use.

## Verification and recorded experiments

The current version passed 52 tests locally and in the Python 3.11–3.13 CI
matrix. A local recovery drill on November 10, 2026 exercised two clients,
a rejected CAS, signed full and incremental backups, restore, replay, and
rejection of tampered data, corrupt archives, and an incorrect checkpoint key.

Published benchmarks include 1, 4, 8, and 16 clients in NORMAL and FULL sync
modes. The mixed HTTP workload measured lower read latency alongside higher
write-tail latency after moving write embeddings outside the database lock.
See [measurements and reproduction commands](docs/SCALING.md).

The development experiments below record earlier runs

- Claude Desktop and the raw client overwrote the same key, with Desktop's
  value lasting 124 ms before another write replaced it.
- A contention run completed approximately 2,500 writes across two clients
  with zero protocol errors.
- A semantic query for appearance preferences retrieved a theme fact whose
  wording differed from the query.
- A previous deployment demonstrated authenticated HTTP access from a laptop
  through a tunnel to a GCP VM.

## Quickstart

Use Python 3.11, 3.12, or 3.13. Run the examples from the repository root.

```sh
git clone https://github.com/yashyegare/mcp-memory-bridge.git
cd mcp-memory-bridge
python -m venv venv
```

For an existing checkout, open its directory and use its existing `venv`.

### Windows PowerShell

Install the lean runtime and open Python

```powershell
.\venv\Scripts\python.exe -m pip install mcp==2.2.0 numpy==2.2.6
$env:MCP_TRANSPORT = "stdio"
$env:MCP_EMBEDDINGS = "off"
.\venv\Scripts\python.exe
```

### Linux and macOS

```sh
venv/bin/python -m pip install mcp==2.2.0 numpy==2.2.6
export MCP_TRANSPORT=stdio
export MCP_EMBEDDINGS=off
venv/bin/python
```

### Launch the memory server through the raw client

At the Python prompt, run

```python
import sys
sys.path.insert(0, "client")
from raw_client import RawMCPClient

client = RawMCPClient([
    sys.executable,
    "server/memory_server.py",
    "local-demo.db"
])
client.initialize()

print([tool["name"] for tool in client.list_tools()])
print(client.call_tool("memory_set", {
    "key": "demo/theme",
    "value": "dark"
})["content"][0]["text"])
print(client.call_tool("memory_get", {
    "key": "demo/theme"
})["content"][0]["text"])

client.close()
```

The client starts the server, initializes MCP, and stores a fact in
`local-demo.db`. The get response should contain `demo/theme = dark`.
Call `exit()` to leave Python. To keep experimenting, leave `client.close()`
until the end of your session. Lean mode supports the memory and recovery
tools. Semantic search requires the optional embedding dependencies.

To demonstrate two clients in the same Python session, create a second
`RawMCPClient` with the same database path, initialize it, and call
`memory_get`. Close both clients when finished.

### Semantic search and the test suite

Install the full requirements using the project environment

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pytest tests/ -q
```

On Linux/macOS, use `venv/bin/python` in place of the Windows interpreter path.
Set `MCP_EMBEDDINGS=1` before launching the server to enable semantic search.
The first embedding operation downloads the model if it is missing from the
local cache.

For the separate protocol-only echo/add smoke test, run
`venv\Scripts\python.exe client/raw_client.py` on Windows or
`venv/bin/python client/raw_client.py` on Linux/macOS.

### Docker

Build the lean image from the repository root

```sh
docker build -t mcp-memory-bridge .
```

An MCP host can launch it with this configuration

```json
{
  "command": "docker",
  "args": [
    "run", "--rm", "-i",
    "-v", "mcp-memory-data:/data",
    "mcp-memory-bridge"
  ]
}
```

The named volume persists the database and WAL files. The image runs as UID/GID
10001. Bind-mounted directories need write access for that identity. CI tests
both the MCP handshake and storage under the non-root user.

In the remaining examples, `python` refers to the project environment.
Use `venv\Scripts\python.exe` on Windows or `venv/bin/python` on Linux/macOS
when invoking commands without activating that environment.

## Project layout

```
client/
  raw_client.py        # hand-rolled MCP clients: stdio + HTTP, handshake, tools/*
server/
  test_server.py       # trivial SDK server (echo, add) — Phase 1 target
  memory_server.py     # SQLite-backed shared memory server — Phases 2/3/4
  audit.py             # versioned hash chain and replay
  checkpoints.py       # authenticated external checkpoints
  recovery.py          # online full backups and event incrementals
  storage.py           # schema and SQLite connection configuration
  embeddings.py        # lazy embedding model and its lock
  http_transport.py    # authentication, reload, operational endpoints
  telemetry.py         # process counters and redacted debug records
tools/
  memory_admin.py      # verify, checkpoint, back up, restore, plan retention
  crash_consistency.py # repeated forced termination and recovery checks
  bench_concurrency.py # stdio write throughput and latency
  bench_mixed_http.py  # embedded writes and unlogged HTTP reads
  stress_concurrent.py # multi-client write race harness
  live_hammer.py       # timed hammer for the live Claude Desktop race
  lock_demo.py         # deterministic lock-contention demo
  inspect_memory.py    # pretty-print the store / history / search log
tests/                 # integration tests: real subprocesses, real wire protocol
deploy/                # systemd unit + one-shot VM setup script
docs/NOTES.md          # the debugging stories: what broke and why
```

## How it's built

### Phase 1 — raw MCP client (no SDK)

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

### Phase 3 — two clients, one database

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

With embeddings enabled, `memory_set` embeds the fact (`"key: value"`, all-MiniLM-L6-v2, 384-dim
float32 BLOB in `fact_embeddings`); `memory_search(query, top_k)` embeds the
query and ranks by cosine similarity in plain numpy — no vector DB. Query
words need not appear in any stored fact. Searches that find a match are audit-logged.

Design notes: the model loads **lazily** on first embedding use (per-process
singleton, single-flight lock) — eager loading would tax every client spawn,
since each MCP client runs its own server subprocess. Once the model is
cached, the server defaults HF Hub to offline mode, respecting an explicit
environment override. `MCP_EMBEDDINGS=off` enables lean mode.
`MCP_PRELOAD_MODEL=1` starts loading the model in a background thread.

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

### HTTP transport and authentication

HTTP mode serves multiple client sessions from one process. For a local
PowerShell demo, generate a shared token, copy it for the client, and start
the server

```powershell
$env:MCP_TRANSPORT = "http"
$env:MCP_EMBEDDINGS = "off"
$env:MCP_HTTP_HOST = "127.0.0.1"
$env:MCP_HTTP_PORT = "8000"
$env:MCP_AUTH_CLIENTS_FILE = ""
$env:MCP_AUTH_TOKEN = .\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:MCP_AUTH_TOKEN
.\venv\Scripts\python.exe server/memory_server.py local-http.db
```

Keep that terminal running. In a second terminal at the repository root, open
`.\venv\Scripts\python.exe` and run

```python
import getpass
import sys
sys.path.insert(0, "client")
from raw_client import RawHTTPMCPClient

client = RawHTTPMCPClient(
    "http://127.0.0.1:8000/mcp",
    token=getpass.getpass("Paste the server token: ")
)
client.initialize()
print([tool["name"] for tool in client.list_tools()])
print(client.call_tool("memory_set", {
    "key": "demo/http",
    "value": "shared over HTTP"
})["content"][0]["text"])
print(client.call_tool("memory_get", {
    "key": "demo/http"
})["content"][0]["text"])
client.close()
```

Stop the HTTP server with Ctrl+C. The bearer token authenticates the caller.
`Mcp-Session-Id` identifies its MCP session. The raw HTTP client preserves that
session header, distinguishes HTTP failures from MCP errors, and uses the SDK
server's JSON response mode.

For server-assigned client identities, configure `MCP_AUTH_CLIENTS_FILE`
instead of `MCP_AUTH_TOKEN`. It accepts token hashes and read or read/write
scopes, and reloads on each request so revocation applies to existing sessions.
Shared-token HTTP and stdio use caller-supplied attribution. Configuration
examples and rotation steps are in [HTTP credentials](docs/AUTH.md).

HTTP serves authenticated `/healthz` and `/metrics` endpoints. The server
retains Host and Origin checks. Set `MCP_ALLOWED_HOSTS` to the hostname used
by a remote proxy, and terminate TLS at that proxy.

### Deployment notes

The latest code was verified locally and in CI. Earlier development included
a lean GCP VM deployment reached through a tunnel. Provider-specific steps from
that experiment are retained in [deployment notes](docs/DEPLOY.md). Check
current provider requirements and pricing when following those historical notes.

Before upgrading an existing database, stop older server processes and take a
pre-upgrade backup. Follow the [migration and recovery procedure](docs/RECOVERY.md).
The code under `deploy/` provides a systemd service and installation script.

## Configuration

Per client, via env — Claude Desktop's `mcpServers.env` works the same way.

| Variable | Meaning |
|---|---|
| db argv / `MCP_MEMORY_DB` | which SQLite file to share |
| `MCP_CLIENT_ID` | attribution for this client's writes and log entries |
| `MCP_SQLITE_BUSY_TIMEOUT` | ms a write waits for the lock (default 5000; `0` = fail fast) |
| `MCP_SQLITE_SYNCHRONOUS` | `NORMAL` (default) or `FULL`, applied to every server connection |
| `MCP_EMBEDDINGS` | `off` = lean server, no ML stack |
| `MCP_PRELOAD_MODEL` | `1` = start loading the embedding model in a background thread |
| `MCP_LOG_READS` | `off` = `memory_get` becomes truly read-only (see design notes) |
| `MCP_MAX_KEY_LEN` / `MCP_MAX_VALUE_LEN` | input caps (defaults 256 / 16384 chars) |
| `MCP_TRANSPORT` | `stdio` (default, one subprocess per client) or `http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | bind address for the HTTP transport (default localhost:8000) |
| `MCP_AUTH_TOKEN` | shared HTTP bearer token, with caller-supplied attribution |
| `MCP_AUTH_CLIENTS_FILE` | path to reloadable client token hashes and read/write scopes. Set exactly one HTTP credential option |
| `MCP_ALLOWED_HOSTS` | comma-separated public hostnames (e.g. a Tailscale Funnel host) allowed past the SDK's DNS-rebinding `Host` check |
| `MCP_ENABLE_DEMO_TOOLS` | `1` = add `lock_hold(seconds)` demo tool for contention demos |
| `MCP_DEBUG_LOG` | path for JSON timing and outcome records with fact contents redacted |

## Audit verification and recovery

```sh
python tools/memory_admin.py verify memory.db --replay
python tools/memory_admin.py head memory.db
python tools/memory_admin.py keygen /secure/checkpoint.key
python tools/memory_admin.py checkpoint memory.db /secure/heads/snapshot.json --key-file /secure/checkpoint.key
python tools/memory_admin.py backup memory.db full.zip --key-file /secure/checkpoint.key
python tools/memory_admin.py incremental memory.db delta.zip --since full.zip --key-file /secure/checkpoint.key
python tools/memory_admin.py restore full.zip recovered.db --incremental delta.zip --key-file /secure/checkpoint.key --expected-head <trusted-head>
```

Versioned audit events commit with facts and vectors. Replay checks every fact's
value, source, timestamp, and embedding against the baseline and subsequent
events. HMAC authenticated checkpoints detect a rewritten or truncated snapshot
when the key and latest checkpoint are protected independently from the database
owner. The first access after upgrade seals the current legacy state as a
baseline. Stop older servers and take a pre-upgrade backup before migrating.

Full backups use SQLite's online backup API. Incrementals contain subsequent
events and changed vectors. Restore verifies checksums, hashes, replay, and
authenticated heads before publishing to a new path. The retention command
plans which complete generations to keep. Detailed upgrade steps, key custody,
recovery commands, and durability limits are in [Recovery](docs/RECOVERY.md).

## Tests and benchmarks

The suite includes 52 tests covering real subprocess and HTTP MCP
traffic, legacy migration, replay tampering, signed checkpoints, incremental
recovery, credential reload, scopes, and forced process termination. The
hung-server regression test confirms that a silent server produces a bounded
client timeout. The semantic lifecycle and latency tests load the real embedding
model.

```sh
python -m pytest tests/ -q
python tools/crash_consistency.py --rounds 10
python tools/bench_concurrency.py --clients 8 --writes 200 --synchronous FULL
python tools/bench_mixed_http.py --clients 4 --operations 100
```

The concurrency benchmark reports throughput, p50, p99, and failures after
warmup. The mixed HTTP benchmark measures embedded writes alongside unlogged
reads in one process. Measurements and reproduction commands are published in
[Scaling the store](docs/SCALING.md).

## Operations

HTTP serves authenticated `GET /healthz` and `GET /metrics`. Readiness checks
database access. Metrics expose HTTP duration sums and counts, tool durations,
server lock wait, authentication failures, chain length, and the active SQLite
sync mode. Fact contents and credentials are omitted. Failed authentication
attempts are logged to stderr. `MCP_DEBUG_LOG` records JSON timing and outcome
metadata.

The Docker image runs as UID/GID 10001. Make bind-mounted data directories
writable by that identity, or mount a Docker-managed volume at `/data`.
The server code is split into storage, audit, recovery, checkpoints, embeddings,
HTTP transport, and telemetry modules.

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
  process, `_SERVER_LOCK` serializes database access while write embeddings compute
  under a separate model lock; (2) by default `memory_get` logs an
  event row, so **reads are writes** — they take the write lock and can
  queue behind another client. `MCP_LOG_READS=off` restores a genuinely
  read-only get at the cost of read attribution.
- **Identity follows the transport.** Stdio and shared-token HTTP accept
  caller-supplied labels. Per-client HTTP assigns identity at authentication
  with read or read/write access to the shared store.
- **Input caps.** Keys and values are length-capped (defaults 256 / 16384)
  because a shared TEXT store with no limits is one `memory_set` away from
  a bloated database.
- **Semantic search without a vector DB.** Embeddings in a plain SQLite
  table with brute-force cosine scoring in numpy. Search scans stored
  vectors. Measured latency and possible indexing changes are covered in
  [Scaling the store](docs/SCALING.md).
- **Known trade-offs.** stdio keeps one server per client (the HTTP
  transport removes that limit when needed); values are plain TEXT;
  `memory_list` uses a prefix `LIKE` query, and write transactions serialize
  across processes.

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
