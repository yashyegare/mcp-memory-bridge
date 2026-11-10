# Scaling the store

## Establish the bottleneck

Run `python tools/bench_concurrency.py --clients 8 --writes 100` from the
repository root with the project environment active. Repeat with 1, 4, 8,
and 16 clients on the same machine. Record throughput, p50, p99, and the
failure count together. The harness uses a fresh database, disables
embeddings, and drives one hot key through independent stdio subprocesses.
It measures client-observed latency, including JSON-RPC and SQLite writer
contention. HTTP, read traffic, embeddings, and a large existing audit log
need separate workload measurements.

## Local measurements on November 8, 2026

These measurements used Windows 10 build 19045, an Intel Core i5-10300H,
Python 3.13.9, SQLite 3.51.0, and the local workstation filesystem. Each
stdio run created a new database and made 200 timed writes per client
after warmup. Percentiles use nearest rank. All 11,600 timed writes in
the following eight runs succeeded.

With `synchronous=NORMAL` the observed results were

- 1 client, 576.98 writes/s, p50 1.563 ms, p99 4.062 ms.
- 4 clients, 924.31 writes/s, p50 1.448 ms, p99 36.365 ms.
- 8 clients, 854.50 writes/s, p50 1.545 ms, p99 47.386 ms.
- 16 clients, 832.22 writes/s, p50 1.719 ms, p99 77.596 ms.

With `synchronous=FULL` the observed results were

- 1 client, 484.99 writes/s, p50 1.961 ms, p99 3.312 ms.
- 4 clients, 562.42 writes/s, p50 1.746 ms, p99 53.428 ms.
- 8 clients, 662.59 writes/s, p50 1.742 ms, p99 76.642 ms.
- 16 clients, 629.13 writes/s, p50 1.816 ms, p99 112.471 ms.

Reproduce each row with

```sh
python tools/bench_concurrency.py --clients 8 --writes 200 --synchronous NORMAL --json
python tools/bench_concurrency.py --clients 8 --writes 200 --synchronous FULL --json
```

These are individual workstation samples. Scheduling, background load, and
storage affect the values, including the tail latency. The measured cost
of FULL is useful context for choosing a durability mode.

## Embedded HTTP writes and reads

The mixed workload uses four sessions in one server process. Two clients
write embedded values while two clients perform unlogged gets. Each client
makes 100 timed requests after loading all-MiniLM-L6-v2. Reads and writes
use the same key, NORMAL sync, and the shared-token HTTP configuration.
Each client stops after its own 100 operations, so the concurrent mix
changes as the readers finish.

Two runs of commit `404e8ec`, before this branch, produced

- Run 1, 111.79 operations/s. Read p50/p99 33.432/50.871 ms.
  Write p50/p99 34.449/52.108 ms.
- Run 2, 105.33 operations/s. Read p50/p99 35.092/55.788 ms.
  Write p50/p99 36.207/57.998 ms.

Two runs of this branch produced

- Run 1, 102.96 operations/s. Read p50/p99 6.383/22.403 ms.
  Write p50/p99 32.854/125.779 ms.
- Run 2, 98.45 operations/s. Read p50/p99 6.214/25.768 ms.
  Write p50/p99 34.043/129.097 ms.

All timed requests succeeded. Write embeddings now compute outside the
database lock while encoding remains serialized by the model lock.
The comparison also includes this branch's additional audit and
authentication work. Read latency fell, writer p99 increased, and aggregate
throughput fell in these runs. The observations describe this particular
workload and include its contention trade-off.

```sh
git worktree add --detach ../mcp-memory-before 404e8ec
python tools/bench_mixed_http.py --server ../mcp-memory-before/server/memory_server.py --clients 4 --operations 100
python tools/bench_mixed_http.py --clients 4 --operations 100
```

## One node

Write embeddings compute outside the server database lock, under a separate
model lock. Fact, vector, and event writes still commit in one transaction.
Search currently holds the database lock while scoring. Independent reader
connections are a further option if measurements justify the added concurrency.
SQLite still allows one writer per database. Logged reads compete for that
writer slot, so evaluate `MCP_LOG_READS=off` if read auditing is optional.
Version 2 set events retain their vectors for incremental recovery, so
audit storage grows with both the write count and vector dimensions.

Semantic search currently loads vectors and scores every candidate in
Python. Measure a SQLite vector extension such as sqlite-vec for in-database
search and use current exact cosine results as the comparison baseline.
If scans still miss the latency target, evaluate an approximate
nearest-neighbor index such as HNSW against that baseline. Measure recall
at k, update and delete behavior, index memory, and rebuild time. If the
index is external, publish changes through a transactional outbox and define
how long search results may lag writes.

## Multiple nodes

Start with a single writable primary in a transactional database and add
read replicas if stale reads are acceptable. For horizontal write capacity,
partition by namespace so fact, CAS, vector metadata, and audit event can
commit on one shard. Hot namespaces can still saturate their shard. Global
prefix listing and semantic search would fan out, merge results, and need
explicit behavior for timeouts or missing shards.

Each shard can maintain its own ordered audit chain and publish trusted
heads independently. A global chain would introduce cross-shard ordering
and another serialization point. Define which acknowledgements require
durable replication and whether failover may lose acknowledged writes.
Plan a coordinated snapshot or per-shard recovery positions before treating
separate snapshots as one recoverable store. At-least-once retries also need
an operation ID if clients require one audit event per logical write.
