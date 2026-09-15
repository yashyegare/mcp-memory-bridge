"""
Standalone concurrency stress harness: drives RawMCPClient instances
against the memory server (Phase 3 lesson, no Claude Desktop needed).

What it does: spawns N clients (each its own server subprocess sharing one
SQLite file), points them all at the same key, and hammers it with writes.
Every writer verifies the write with an immediate memory_get and records
what it observed, so the output shows last-write-wins interleavings -- and
the attribution column shows exactly who overwrote whom, which is the point
of the events table.

Usage:
    venv\\Scripts\\python.exe tools/stress_concurrent.py [--clients 2]
        [--writes 40] [--key stress/key] [--db stress.db]

Note: each client spawns its own server subprocess; they coordinate purely
through SQLite's WAL mode + busy_timeout, which is exactly the multi-client
setup Claude Desktop would create (one subprocess per client, same file).
"""

import argparse
import sys
from collections import Counter

sys.path.insert(0, "client")
from raw_client import MCPError, RawMCPClient  # noqa: E402

DEFAULT_DB = "stress.db"
PYTHON = sys.executable


def run_writer(writer_id: str, db_path: str, key: str, n_writes: int) -> dict:
    """One client process-worth of writes: set key=i then immediately get it."""
    client = RawMCPClient([PYTHON, "server/memory_server.py", db_path])
    observed: list[int] = []
    errors: list[str] = []
    try:
        client.initialize()
        for i in range(n_writes):
            value = f"{writer_id}-write-{i}"
            try:
                client.call_tool("memory_set", {"key": key, "value": value, "client_id": writer_id})
                result = client.call_tool("memory_get", {"key": key})
                # Parse our own value back out of the get result, or record
                # whatever someone else's value we saw instead.
                text = "\n".join(
                    b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"
                )
                if value in text:
                    observed.append(i)
                else:
                    observed.append(-1)  # saw someone else's write land on top
            except (MCPError, ConnectionError) as exc:
                errors.append(f"write {i}: {type(exc).__name__}: {exc}")
    finally:
        client.close()
    return {"writer": writer_id, "observed": observed, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument("--writes", type=int, default=40)
    parser.add_argument("--key", default="stress/shared-key")
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()

    print(f"spawning {args.clients} clients x {args.writes} writes on key {args.key!r} (db: {args.db})")

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.clients) as pool:
        results = list(
            pool.map(
                lambda i: run_writer(f"client-{i}", args.db, args.key, args.writes),
                range(args.clients),
            )
        )

    total_errors = sum(len(r["errors"]) for r in results)
    total_overwritten = sum(1 for r in results for o in r["observed"] if o == -1)

    for r in results:
        seen = Counter(r["observed"])
        ok = sum(1 for o in r["observed"] if o >= 0)  # -1 marks "another client's value"
        print(
            f"  {r['writer']}: {ok}/{args.writes} reads saw own write, "
            f"{seen.get(-1, 0)} reads saw another client's value, "
            f"{len(r['errors'])} errors"
        )
        for err in r["errors"][:3]:
            print(f"    {err}")

    print()
    print(f"totals: {total_overwritten} reads observed another client's write win; {total_errors} transport/protocol errors")
    print("(0 errors + some overwritten reads = WAL queued everyone cleanly and last-write-wins decided it)")
    print("inspect the audit trail:  venv/Scripts/python.exe -c \"import sqlite3; [print(*r, sep=' | ') for r in sqlite3.connect('" + args.db + "').execute('SELECT id,timestamp,client_id,action,value FROM events ORDER BY id')]\"")


if __name__ == "__main__":
    main()
