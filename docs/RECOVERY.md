# Audit verification and recovery

Run the commands from the repository root with the project Python environment
active. Use a separate account or backup host for checkpoint operations.

## Upgrade existing databases

Stop every older server process and take a pre-upgrade copy of the database
using the previous release's online backup command. Open the database with the
new server and run a memory tool or an authenticated health check to initialize
the schema.

The migration verifies any existing hash chain, then atomically records a
baseline containing the current facts, exact timestamps, and embeddings.
Version 2 events record enough information to replay subsequent changes.
Older writers are rejected by a database trigger. A database without historical
hashes gets its existing history sealed first. Migration accepts the current
legacy state as its starting point. Pre-upgrade changes require an independently
trusted older backup or checkpoint for comparison.

## Replay and authenticated checkpoints

```sh
python tools/memory_admin.py verify memory.db --replay
python tools/memory_admin.py head memory.db
python tools/memory_admin.py keygen /secure/checkpoint.key
python tools/memory_admin.py checkpoint memory.db /secure/heads/snapshot-001.json --key-file /secure/checkpoint.key
python tools/memory_admin.py verify memory.db --checkpoint /secure/heads/snapshot-001.json --key-file /secure/checkpoint.key
```

Replay compares every fact's value, source, and timestamp with the state
reconstructed from the sealed baseline and events. It also compares embeddings.
A lean write removes an existing vector for its key, keeping the stored vector
consistent with the new value.

Each checkpoint authenticates the store identity, event count, last event ID,
and head with HMAC-SHA256. Checkpoint verification requires an exact snapshot
match and includes replay. After legitimate new writes, capture a new checkpoint.
The incremental backup command can validate an older checkpoint as a prefix.

When advancing an existing trust anchor, pass `--previous protected-earlier.json`
to `checkpoint`. For a later full backup, pass `--checkpoint protected-earlier.json`.
Both commands require the earlier signed head to remain a valid prefix. This
keeps the signing workflow from blessing changes to already checkpointed
history. A first capture establishes trust in its current snapshot.

The generated key contains 32 random bytes. Back it up separately and restrict
it to the checkpoint operator. Put checkpoint records in a directory controlled
by that operator, or copy them into storage with enforced append-only retention.
The server account and database owner must have no write access to those
records and no access to the key. Local files inherit directory access controls
on Windows. On POSIX, newly published keys, checkpoints, and archives use mode
0600.

With an independently protected latest checkpoint, a database-only attacker
cannot authenticate a rewritten chain or a truncated snapshot. An ordinary
unanchored hash check permits a valid earlier prefix. Checkpoints authenticate
the state at capture time. Changes made before their first protected capture
remain outside that guarantee. Protecting the latest record also prevents rollback
to an older signed checkpoint. The server does not need the checkpoint key.

## Full and incremental backups

```sh
python tools/memory_admin.py backup memory.db full-001.zip --key-file /secure/checkpoint.key
python tools/memory_admin.py incremental memory.db delta-001.zip --since full-001.zip --key-file /secure/checkpoint.key
python tools/memory_admin.py incremental memory.db delta-002.zip --since delta-001.zip --key-file /secure/checkpoint.key
python tools/memory_admin.py restore full-001.zip recovered.db --incremental delta-001.zip --incremental delta-002.zip --key-file /secure/checkpoint.key --expected-head <trusted-final-head>
python tools/memory_admin.py verify recovered.db --replay --expected-head <trusted-final-head>
```

A full backup captures a consistent SQLite online snapshot, including committed
WAL data. Its manifest contains the checkpoint for that exact snapshot.
An incremental contains only events after its predecessor's recorded ID,
including the vectors attached to changed facts. It checks the predecessor's
head against the live chain before exporting.

Restore requires a full archive followed by incrementals in order. It rejects
gaps, duplicates, a different predecessor, invalid hashes, invalid HMACs, and a
mismatched expected final head. It replays fact and vector changes and checks
the resulting state before publishing to a new database path. Existing files
and SQLite sidecars cause restore to fail. The original archive format remains
readable. Archives from before the version 2 upgrade receive their original
chain and checksum verification.

Supply `--key-file` during both backup creation and restore to authenticate the
archive head. Keep the expected final head or signed manifest under independent
control. Omitting a key permits the compatible checksum-only workflow for
unsigned archives. The destination filesystem must support hard links. On POSIX,
publication also syncs the destination directory.

To switch databases, stop the server, point its configured DB path at the
recovered file, and restart it. Preserve the full backup and every incremental
needed to reach a recovery point.

## Retention policy

```sh
python tools/memory_admin.py retention /backups --keep-full 3 --key-file /secure/checkpoint.key
```

Keep the newest three full snapshots per store, ordered by audit position, and
all incremental descendants reachable from those snapshots. The command prints
the retained names and retirement candidates. It produces a reviewable plan.
It leaves file deletion to the operator. Equal-position full snapshots represent
the same recovery state and use the filename to break ties. Legacy archives
without manifests must be managed separately.

For signed archives, the planner requires the checkpoint key and validates
manifest signatures. Archive contents still require a restore drill before
retirement decisions.

Before retiring a generation, test recovery of a retained generation, preserve
the checkpoint key, and account for any required long-term recovery points.

## Durability and crash checks

Set `MCP_SQLITE_SYNCHRONOUS=FULL` for a WAL sync on each commit or use the default
`NORMAL` for lower write latency. `NORMAL` can lose recent acknowledged commits
after power loss. `FULL` strengthens commit durability subject to the operating
system and storage device honoring sync requests.

```sh
python tools/crash_consistency.py --rounds 10 --synchronous both
```

The harness repeatedly kills a real server during an active write loop,
reopens the same database, and checks SQLite integrity, hashes, replay, and every
acknowledged write from that round. It targets the interpreter PID, including
when a Windows venv launcher introduces a parent process. Linux uses SIGKILL
and Windows uses forced process termination. On November 8, 2026, ten cycles
per mode passed locally on Windows, and three cycles per mode passed in the
non-root Linux container. Process termination exercises recovery
from an interrupted process. Power-loss testing requires a separate fault
environment that also exercises the OS and device caches.
