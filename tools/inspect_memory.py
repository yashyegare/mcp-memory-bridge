"""
inspect_memory: pretty-print the memory server's SQLite store directly
(no MCP involved -- handy for demos and debugging).

    venv\\Scripts\\python.exe tools\\inspect_memory.py                  # overview
    venv\\Scripts\\python.exe tools\\inspect_memory.py --key live/color # one key's history
    venv\\Scripts\\python.exe tools\\inspect_memory.py --full           # every event ever
    venv\\Scripts\\python.exe tools\\inspect_memory.py --searches      # semantic search log

Reads the db read-only-ish (plain SELECTs); safe to run while servers are
writing thanks to WAL mode.
"""

import argparse
import sqlite3
import sys

SCHEMA_NOTE = "expected tables missing -- point --db at the memory server's file"


def overview(db: sqlite3.Connection) -> None:
    print("=== FACTS ===")
    rows = db.execute("SELECT key, value, source_client, updated_at FROM facts ORDER BY key").fetchall()
    if not rows:
        print("  (empty)")
    for key, value, source, updated in rows:
        print(f"  {key} = {value}")
        print(f"      source: {source}  updated: {updated}")
    print()

    print("=== EMBEDDINGS ===")
    n = db.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
    print(f"  {n} fact(s) embedded (Phase 4A semantic recall)")
    print()

    print("=== ACTIVITY BY CLIENT ===")
    for client, action, count in db.execute(
        "SELECT client_id, action, COUNT(*) FROM events GROUP BY client_id, action ORDER BY client_id, action"
    ):
        print(f"  {client:>16} {action:>8}: {count}")


def key_history(db: sqlite3.Connection, key: str) -> None:
    fact = db.execute("SELECT value, source_client, updated_at FROM facts WHERE key = ?", (key,)).fetchone()
    print(f"=== HISTORY: {key} ===")
    if fact:
        print(f"  current: {fact[0]}  (source: {fact[1]}, updated: {fact[2]})")
    else:
        print("  (key does not currently exist -- history below is what remains)")
    print()
    rows = db.execute(
        "SELECT timestamp, client_id, action, value FROM events "
        "WHERE key = ? AND action IN ('set', 'delete', 'search') ORDER BY id",
        (key,),
    ).fetchall()
    if not rows:
        print("  (no recorded events)")
    for timestamp, client, action, value in rows:
        print(f"  {timestamp}  {client:>16}  {action:>7}  {value}")


def full_log(db: sqlite3.Connection, limit: int) -> None:
    print(f"=== FULL EVENT LOG (last {limit}) ===")
    rows = db.execute(
        "SELECT id, timestamp, client_id, action, key, value FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    for eid, timestamp, client, action, key, value in reversed(rows):
        keypart = f" {key}" if key else ""
        valpart = f" = {value}" if value is not None else ""
        print(f"  #{eid} {timestamp} {client:>16} {action:>8}{keypart}{valpart}")


def searches(db: sqlite3.Connection, limit: int) -> None:
    print(f"=== SEMANTIC SEARCH LOG (last {limit}) ===")
    rows = db.execute(
        "SELECT timestamp, client_id, key, value FROM events "
        "WHERE action = 'search' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        print("  (no searches recorded yet)")
    for timestamp, client, matched_key, detail in reversed(rows):
        print(f"  {timestamp}  {client:>16}  matched {matched_key}")
        print(f"      {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="memory.db", help="path to the SQLite file")
    parser.add_argument("--key", help="show full history for one key")
    parser.add_argument("--full", action="store_true", help="dump the entire event log")
    parser.add_argument("--searches", action="store_true", help="show the semantic search log")
    parser.add_argument("--limit", type=int, default=200, help="row limit for --full / --searches")
    args = parser.parse_args()

    try:
        db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)  # read-only: cannot corrupt anything
    except sqlite3.OperationalError as exc:
        sys.exit(f"cannot open {args.db!r}: {exc}")
    db.row_factory = sqlite3.Row

    try:
        if args.key:
            key_history(db, args.key)
        elif args.searches:
            searches(db, args.limit)
        elif args.full:
            full_log(db, args.limit)
        else:
            overview(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
