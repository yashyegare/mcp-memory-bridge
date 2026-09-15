"""
Shared-memory MCP server (Phase 2): SQLite-backed key/value facts with a
full audit trail, exposed over stdio via the official SDK (mcp 2.x).

## Conflict policy: LAST-WRITE-WINS, deliberately

Concurrent writes to the same key do not conflict-check; the last commit
wins and every intermediate write is preserved in `events`, so an
overwrite is always detectable and attributable after the fact (see
`memory_get`'s `include_events` flag). Rationale:

- With SQLite's busy_timeout (below), writers queue instead of erroring,
  so an explicit conflict check would have to be built *on top of* a lock
  that has already serialized the writes -- the lock, not the check,
  decides the winner. Rejecting at that point adds friction without
  adding information.
- What callers actually need to debug a shared store is attribution and
  history, not early rejection. The events table provides both; if a
  use case later needs optimistic concurrency, compare-and-swap
  (memory_set with expected_value) is the natural extension.

Writes are attributed: every fact row carries source_client, and every
mutation appends to `events`. `busy_timeout` is set high (5s) so
near-simultaneous writers from different clients queue rather than
fail with "database is locked"; WAL mode lets readers proceed while a
write transaction is open.

Run it (usually via your client, not directly):
    venv\\Scripts\\python.exe server/memory_server.py [path/to/memory.db]
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

# mcp 2.x note: FastMCP was renamed to MCPServer (mcp 1.x import fails).
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "memory.db"

mcp = MCPServer("memory-server")

# One connection per server process. The SDK serves all sessions of this
# subprocess on one event loop, so a single connection with a busy timeout
# serializes writes correctly; WAL mode (below) keeps readers unblocked.
_conn: sqlite3.Connection | None = None


def _now() -> str:
    """UTC timestamp, sortable, millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=5.0)  # busy_timeout, in seconds
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")      # readers don't block the writer
        _conn.execute("PRAGMA synchronous=NORMAL;")    # WAL-safe durability trade-off
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts (
                key           TEXT PRIMARY KEY,
                value         TEXT NOT NULL,
                source_client TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                client_id TEXT NOT NULL,
                action    TEXT NOT NULL,   -- 'set' | 'get' | 'delete'
                key       TEXT,
                value     TEXT
            );
            """
        )
        _conn.commit()
    return _conn


def _log(client_id: str, action: str, key: str | None, value: str | None) -> None:
    _db().execute(
        "INSERT INTO events (timestamp, client_id, action, key, value) VALUES (?, ?, ?, ?, ?)",
        (_now(), client_id, action, key, value),
    )


@mcp.tool()
def memory_set(key: str, value: str, client_id: str = "anonymous") -> str:
    """Store a fact (last-write-wins). Overwrites any existing value for
    `key`; the write is attributed to `client_id` and logged to events."""
    db = _db()
    with db:  # transaction: fact update + event insert commit or roll back together
        db.execute(
            "INSERT INTO facts (key, value, source_client, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "source_client=excluded.source_client, updated_at=excluded.updated_at",
            (key, value, client_id, _now()),
        )
        _log(client_id, "set", key, value)
    return f"set {key!r} (from {client_id})"


@mcp.tool()
def memory_get(key: str, include_events: bool = False) -> str:
    """Fetch a fact by exact key. With include_events=True, appends the
    key's full write history from the events log."""
    row = _db().execute("SELECT value, source_client, updated_at FROM facts WHERE key = ?", (key,)).fetchone()
    _log("anonymous", "get", key, None)  # reads are logged too, unattributed by default
    _db().commit()
    if row is None:
        # ToolError => isError=true with this message intact (mcp 2.x drops
        # the message of arbitrary exceptions; anticipated errors survive).
        raise ToolError(f"no fact for key {key!r}")
    out = f"{key} = {row['value']}  (source: {row['source_client']}, updated: {row['updated_at']})"
    if include_events:
        rows = _db().execute(
            "SELECT timestamp, client_id, action, value FROM events "
            "WHERE key = ? AND action IN ('set', 'delete') ORDER BY id",
            (key,),
        ).fetchall()
        out += "\nevents:\n" + "\n".join(
            f"  {r['timestamp']} {r['client_id']} {r['action']}: {r['value']}" for r in rows
        )
    return out


@mcp.tool()
def memory_list(prefix: str = "") -> str:
    """List facts whose key starts with `prefix` (all facts if empty)."""
    rows = _db().execute(
        "SELECT key, value, source_client, updated_at FROM facts "
        "WHERE key LIKE ? ORDER BY key",
        (prefix.replace("%", r"\%").replace("_", r"\_") + "%",),
    ).fetchall()
    if not rows:
        return f"(no facts matching prefix {prefix!r})"
    return "\n".join(f"{r['key']} = {r['value']}  (source: {r['source_client']})" for r in rows)


@mcp.tool()
def memory_delete(key: str, client_id: str = "anonymous") -> str:
    """Delete a fact by key. No-op if the key doesn't exist (still logged)."""
    db = _db()
    with db:
        cur = db.execute("DELETE FROM facts WHERE key = ?", (key,))
        _log(client_id, "delete", key, None)
    if cur.rowcount == 0:
        return f"nothing to delete for {key!r} (logged from {client_id})"
    return f"deleted {key!r} (from {client_id})"


if __name__ == "__main__":
    # Avoid stray print()s: stdout is the protocol channel.
    print(f"memory server: db={os.path.abspath(DB_PATH)}", file=sys.stderr)
    mcp.run(transport="stdio")
