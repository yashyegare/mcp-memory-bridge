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

## One node

Within a process, the server lock covers embedding computation as well as
database work. Start by timing those phases separately. Moving computation
outside the lock and giving independent readers their own connections can
reduce contention. Keep fact, vector, and event writes in one transaction.
SQLite still allows one writer per database. Logged reads compete for that
writer slot, so evaluate `MCP_LOG_READS=off` if read auditing is optional.

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
