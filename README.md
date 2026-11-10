# mcp-memory-bridge

[![tests](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/yashyegare/mcp-memory-bridge/actions/workflows/tests.yml)

**The problem:** MCP clients (Claude Desktop, Cursor, custom agents) each
spawn their own tools — nothing shares memory between them. This project
builds a **shared-memory MCP server**: one SQLite store that multiple
independent clients read and write concurrently, with attribution and an
audit trail for every change — plus a **from-scratch MCP client** (raw
JSON-RPC 2.0, no SDK) used to exercise it over both stdio and HTTP.

**Two transport modes, one store:**

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

HTTP mode — one long-running server hosts many remote clients (LIVE):
```

```
 ┌────────────┐  HTTPS  ┌────────────────────────────────────────┐
 │ any MCP    │────┬───►│ GCP e2-micro (always free, no public   │
 │ client     │    │    │ IP, zero open ports)                   │
 │ anywhere   │    │    │  Tailscale Funnel — outbound-only      │
 └────────────┘    │    │  tunnel, TLS at the edge               │
 ┌────────────┐    │    │   └─ memory_server.py (HTTP mode,      │
 │ raw_client │────┘    │       bearer-token gated)              │
 └────────────┘         │       └─ SQLite (WAL), same schema     │
                        └────────────────────────────────────────┘
```

Built to understand the protocol and its concurrency behavior by hand, not
through libraries that hide the interesting parts. **The HTTP mode is not a
diagram**: a GCP always-free e2-micro is running it right now, reachable
over a stable public HTTPS URL — [Deployed on GCP free tier](#deployed-on-gcp-free-tier)
below is the complete, verified walkthrough.

## Results at a glance

| Experiment | Outcome |
|---|---|
| Hand-rolled client | Full handshake, `tools/list`, `tools/call` over raw stdio framing — zero SDK imports |
| Live race: Claude Desktop vs raw client | Desktop's `magenta` write landed mid-stream, held the key for **124 ms**, then lost to last-write-wins — and Desktop *read back the value that replaced it* |
| Contention | ~2,500 writes across two independent clients, **0 protocol errors** (WAL + busy_timeout + LWW) |
| Semantic recall | Query *"appearance preference for screens"* — words absent from every stored fact — still retrieved `user/theme` at cosine 0.427 (unrelated fact: 0.036) |
| Remote, over the public internet | `RawHTTPMCPClient` on a Windows laptop → GCP VM via Tailscale Funnel: handshake, `memory_set`, attributed in the same events table — with auth enforced end to end |
| Warm latency, enforced by tests | `memory_set` ~10 ms (limit 100), `memory_search` ~10 ms (limit 50) |

## Quickstart

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt                  # Windows
source venv/bin/activate && pip install -r requirements.txt   # Linux/macOS

# Integration tests: real subprocesses, real wire protocol
venv\Scripts\python.exe -m pytest tests\ -v                   # Windows
venv/bin/python -m pytest tests/ -v                           # Linux/macOS

# Phase 1 smoke test: hand-rolled client vs the trivial SDK server
venv\Scripts\python.exe client\raw_client.py                  # Windows
venv/bin/python client/raw_client.py                          # Linux/macOS
```

All commands below use `venv\Scripts\python.exe` (Windows); on Linux/macOS
use `venv/bin/python`.

**Docker (lean mode).** The server ships as an image with the ML stack
excluded (`MCP_EMBEDDINGS=off` baked in) — useful when a host prefers
launching a container as its stdio subprocess:

```json
{ "command": "docker",
  "args": ["run", "--rm", "-i", "-v", "mcp-memory-data:/data",
            "mcp-memory-bridge", "python", "server/memory_server.py", "/data/memory.db"] }
```

The named volume persists the database and its WAL files. The image runs as
UID/GID 10001. CI builds the image and tests both the handshake and storage.

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

### Phase 4C — HTTP transport + auth

stdio gives every client its own server subprocess. `MCP_TRANSPORT=http`
flips the server to **streamable-HTTP** so one long-running server hosts any
number of remote clients on the same store — the shape you'd actually
deploy:

```bash
# server (fail-closed: no token, no start)
MCP_TRANSPORT=http MCP_AUTH_TOKEN=<secret> \
  venv/Scripts/python.exe server/memory_server.py memory.db

# client — RawHTTPMCPClient, also stdlib-only, no SDK
venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'client'); from raw_client import RawHTTPMCPClient; c=RawHTTPMCPClient('http://127.0.0.1:8000/mcp', token='<secret>'); c.initialize(); print([t['name'] for t in c.list_tools()]); print(c.call_tool('memory_set',{'key':'net/hello','value':'over http','client_id':'http'}).get('content')[0].get('text')); c.close()"
```

What the HTTP mode teaches that stdio hides (both sides hand-rolled over
`urllib`): identity is a *header* (`Mcp-Session-Id` is issued at initialize
and echoed thereafter), HTTP status codes live a layer below JSON-RPC (401
before any `error` object exists), and `json_response=True` turns the
streamable-HTTP transport's default SSE stream into plain JSON bodies a raw
client can parse.

**Auth design.** The stdio server never needed auth — the OS *is* the
authentication: a host spawns the server as a child process, so only
something with filesystem access could talk to it, and anything with
filesystem access could open the SQLite file directly. A listening socket
changes that completely. The scheme:

- **Bearer authentication.** HTTP requires exactly one of
  `MCP_AUTH_TOKEN` or `MCP_AUTH_CLIENTS_FILE`. The server checks the
  `Authorization: Bearer <token>` header before processing MCP requests.
  Missing or invalid credentials receive `401`.
- **Attribution.** Per-client tokens bind each HTTP session to a configured
  identity. The server uses that identity for fact sources and audit events,
  overriding the optional `client_id` tool argument. Shared-token HTTP and
  stdio keep self-reported attribution. See [HTTP credentials](docs/AUTH.md)
  for setup, token rotation, and revocation.
- **Why not the SDK's OAuth machinery.** mcp 2.x ships full OAuth 2.1
  resource-server support — right tool for public multi-tenant, ~10× the
  moving parts a single-operator demo needs.
- **DNS-rebinding protection vs tunnels.** The SDK's streamable-HTTP app
  validates the `Host` header and defaults to localhost-only, so a request
  arriving via *any* public tunnel is rejected with `421` before auth or
  the MCP layer. Correct behavior, not a tunnel bug: the public hostname
  must be allow-listed via `MCP_ALLOWED_HOSTS` (comma-separated), which
  extends the protection rather than disabling it.

### Deployed on GCP free tier

The live instance: one always-free **e2-micro** VM (us-west1) running the
**lean** server (`MCP_EMBEDDINGS=off` — the embedding model wants ~500 MB
and the VM has 1 GB RAM; semantic search stays a laptop feature) in HTTP
mode, reachable through a **Tailscale Funnel** — no public IP, no open
firewall ports, no domain to buy, a stable HTTPS URL with auto-provisioned
TLS. (A Cloudflare Tunnel works identically if you'd rather; it needs a
domain for a stable URL, which is why Tailscale won. Funnel is beta —
fine for a demo, worth knowing if you ever need guarantees.)

**Bill safety, in order of appearance:** (1) a Free Trial billing account
*cannot charge you* — when the 90-day/$300 trial lapses, resources stop;
charging starts only if you manually upgrade. (2) Even on a paid account
this deploy uses only Always-Free resources: e2-micro hours in
us-central1/us-east1/us-west1 + 30 GB standard disk + ~1 GB egress — and
**no external IP**, the one thing that would cost extra. (3) The 100%
guarantee is deleting the project (see teardown).

**Step 0 — console (~10 min).** Pick or create the project; **Billing →
Budgets & alerts**: amount `$1`, thresholds 50/90/100%, your email — the
alarm bell rings at $0.50; note the project ID.

**Step 1 — the VM (~10 min).** Compute Engine → Create instance:

| Field | Value |
|---|---|
| Name | `memory-server` |
| Region | `us-west1` — always-free; us-central1/us-east1 also OK |
| Machine type | **e2-micro** (2 vCPU shared, 1 GB RAM) |
| Boot disk | **Standard** persistent disk, **30 GB**, **Debian 12** |
| Firewall | leave **both boxes unchecked** — we open nothing |

Then instance → Edit → Network interfaces → default → **External IP →
None**. Or create it right the first time:

```bash
gcloud compute instances create memory-server \
  --zone=us-west1-b --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard --no-address
```

**Step 2 — code + service on the VM (~10 min).** In the browser-SSH window:

```bash
sudo apt-get update && sudo apt-get install -y git python3-venv curl
git clone https://github.com/yashyegare/mcp-memory-bridge.git
cd mcp-memory-bridge
sudo bash deploy/setup.sh        # creates 'memory' user, /opt/mcp-memory/repo,
                                 # lean venv (mcp+numpy, no torch), systemd unit
openssl rand -hex 32             # your bearer token

sudo tee /opt/mcp-memory/env >/dev/null <<EOF
MCP_TRANSPORT=http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_AUTH_TOKEN=<paste-your-token>
MCP_EMBEDDINGS=off
MCP_CLIENT_ID=gcp-server
EOF
sudo chmod 600 /opt/mcp-memory/env && sudo chown memory:memory /opt/mcp-memory/env
sudo systemctl restart memory-server && sudo systemctl status memory-server --no-pager
# want: active (running). "active then inactive (dead), status=0/SUCCESS" means
# it booted in stdio mode — MCP_TRANSPORT=http didn't reach the env file.

curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# expect: 401 — auth layer live; we sent no token
```

**Step 3 — Tailscale (~5 min).** Still on the VM:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up     # open the printed login URL on your LAPTOP; free plan
sudo tailscale status # memory-server active with a 100.x.x.x tailnet IP
```

**Step 4 — Funnel + allow the hostname (~5 min).**

```bash
sudo tailscale funnel --bg 8000      # --bg persists across reboots/re-logins
sudo tailscale funnel status         # prints your URL: https://<device>.<tailnet>.ts.net
```

Before testing from outside, allow that exact hostname (DNS-rebinding
protection, above) and restart:

```bash
sudo sed -i '/MCP_CLIENT_ID/a MCP_ALLOWED_HOSTS=<your-host>.ts.net' /opt/mcp-memory/env
sudo systemctl restart memory-server
```

**Step 5 — prove it from your laptop.**

```powershell
venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'client'); from raw_client import RawHTTPMCPClient; c=RawHTTPMCPClient('https://YOUR-HOST.ts.net/mcp', token='YOUR-TOKEN'); c.initialize(); print([t['name'] for t in c.list_tools()]); print(c.call_tool('memory_set',{'key':'cloud/hello','value':'written from my laptop','client_id':'laptop'}).get('content')[0].get('text')); c.close()"
```

A tool list plus a successful `memory_set` means genuinely reachable from
the public internet, over a stable URL, with auth enforced. The two-machine
demo: this client while a second machine hammers the same key —
`tools/inspect_memory.py` on the VM shows the interleaved, attributed
writes from genuinely different machines.

**Day-2 ops (on the VM):**

```bash
sudo systemctl status memory-server        # is the API up?
sudo journalctl -u memory-server -n 50     # last 50 log lines
sudo systemctl restart memory-server       # after code or env changes
sudo tailscale funnel status               # Funnel still on? URL?
cd ~/mcp-memory-bridge && git pull && sudo bash deploy/setup.sh   # update
```

**Rotating the token** (anyone saw it — screen-share, pasted log, chat):

```bash
openssl rand -hex 32
sudo sed -i 's/MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=<new-token>/' /opt/mcp-memory/env
sudo systemctl restart memory-server
```

**Teardown — the only 100% guarantee.** When the demo has served (or day
~55 of the trial): delete the VM (ends the Funnel with it), then IAM &
Admin → Settings → **Shut down** deletes the whole project after ~30 days;
optionally close the billing account and remove the payment method. A
calendar reminder for **day ~55** saying "teardown" is the most reliable
ops tool in this README.

## Configuration

Per client, via env — Claude Desktop's `mcpServers.env` works the same way.

| Variable | Meaning |
|---|---|
| db argv / `MCP_MEMORY_DB` | which SQLite file to share |
| `MCP_CLIENT_ID` | attribution for this client's writes and log entries |
| `MCP_SQLITE_BUSY_TIMEOUT` | ms a write waits for the lock (default 5000; `0` = fail fast) |
| `MCP_SQLITE_SYNCHRONOUS` | `NORMAL` (default) or `FULL`, applied to every server connection |
| `MCP_EMBEDDINGS` | `off` = lean server, no ML stack |
| `MCP_PRELOAD_MODEL` | `1` = load the embedding model at spawn instead of first use |
| `MCP_LOG_READS` | `off` = `memory_get` becomes truly read-only (see design notes) |
| `MCP_MAX_KEY_LEN` / `MCP_MAX_VALUE_LEN` | input caps (defaults 256 / 16384 chars) |
| `MCP_TRANSPORT` | `stdio` (default, one subprocess per client) or `http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | bind address for the HTTP transport (default localhost:8000) |
| `MCP_AUTH_TOKEN` | shared HTTP bearer token, with caller-supplied attribution |
| `MCP_AUTH_CLIENTS_FILE` | path to reloadable client token hashes and read/write scopes. Set exactly one HTTP credential option |
| `MCP_ALLOWED_HOSTS` | comma-separated public hostnames (e.g. a Tailscale Funnel host) allowed past the SDK's DNS-rebinding `Host` check — see Phase 4C |
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
  table, brute-force cosine in numpy — sub-10 ms at hundreds to low
  thousands of facts, zero extra infrastructure. An ANN index is the
  upgrade path if the store grows, not a day-one need.
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
