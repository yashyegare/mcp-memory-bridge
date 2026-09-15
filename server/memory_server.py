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

import functools
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

# mcp 2.x note: FastMCP was renamed to MCPServer (mcp 1.x import fails).
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# Config via argv > env > defaults, so Claude Desktop / any launcher can
# configure this server without touching code.
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_MEMORY_DB", "memory.db")
BUSY_TIMEOUT_MS = int(os.environ.get("MCP_SQLITE_BUSY_TIMEOUT", "5000"))
DEFAULT_CLIENT_ID = os.environ.get("MCP_CLIENT_ID", "anonymous")

mcp = MCPServer("memory-server")

# One connection per server process. The SDK serves all sessions of this
# subprocess on one event loop, so a single connection with a busy timeout
# serializes writes correctly; WAL mode (below) keeps readers unblocked.
#
# THREADING BUG (found live, via MCP_DEBUG_LOG): the SDK dispatches tool
# calls onto different worker threads under load. python's sqlite3 forbids
# sharing a connection across threads by default, so any call landing on a
# thread other than the connection's creator crashed with ProgrammingError
# ("SQLite objects created in a thread can only be used in that same
# thread") -- and worse, it only failed SOMETIMES, because early low-traffic
# calls happened to land on the creating thread. Fix: check_same_thread=
# False + one lock serializing every tool call (python sqlite3 connections
# still need user-level serialization; this also matches SQLite's
# single-writer model, so it costs nothing in practice).
_conn: sqlite3.Connection | None = None
_SERVER_LOCK = threading.RLock()


def _now() -> str:
    """UTC timestamp, sortable, millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # busy_timeout: how long a write waits for the lock before raising
        # "database is locked". 0ms = fail fast (for experiments), default
        # 5s = queue concurrent writers (the production setting).
        _conn = sqlite3.connect(
            DB_PATH,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,  # SDK dispatches calls across threads; _SERVER_LOCK serializes
        )
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


def _tool_guard(fn):
    """Two jobs:

    1. Trace every tool call (args, outcome, exceptions) to the file named
       by MCP_DEBUG_LOG when set -- our forensic recorder for client-side
       sessions whose stderr we can't see (e.g. Claude Desktop).
    2. mcp 2.x flattens arbitrary tool exceptions to 'Error executing tool
       <name>', losing the message entirely. ToolError survives -- the SDK
       prefixes it ('Error executing tool <name>: ...') but keeps the
       detail. Translate lock errors into ToolError so clients can read
       and act on them.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        debug_path = os.environ.get("MCP_DEBUG_LOG")
        start = time.time()
        try:
            with _SERVER_LOCK:  # serialize all db access across SDK worker threads
                result = fn(*args, **kwargs)
            if debug_path:
                with open(debug_path, "a", encoding="utf-8") as f:
                    f.write(f"{_now()} OK   {fn.__name__} args={args} kwargs={kwargs} "
                            f"({time.time() - start:.3f}s) -> {str(result)[:200]}\n")
            return result
        except sqlite3.OperationalError as exc:  # outside the lock: nothing db-related to serialize
            if debug_path:
                import traceback
                with open(debug_path, "a", encoding="utf-8") as f:
                    f.write(f"{_now()} FAIL {fn.__name__} args={args} kwargs={kwargs} "
                            f"({time.time() - start:.3f}s) {type(exc).__name__}: {exc}\n")
            if "locked" in str(exc) or "busy" in str(exc):
                raise ToolError(
                    f"database is locked (busy_timeout={BUSY_TIMEOUT_MS}ms): {exc}. "
                    "Another client holds the write lock; retry with backoff."
                ) from None
            raise
        except BaseException as exc:  # record everything, then surface it
            if debug_path:
                import traceback
                with open(debug_path, "a", encoding="utf-8") as f:
                    f.write(f"{_now()} FAIL {fn.__name__} args={args} kwargs={kwargs} "
                            f"({time.time() - start:.3f}s) {type(exc).__name__}: {exc}\n"
                            f"{''.join(traceback.format_exc())}\n")
            raise

    return wrapper


@_tool_guard
def _guarded_set(key: str, value: str, client_id: str | None = None) -> str:
    cid = client_id or DEFAULT_CLIENT_ID
    db = _db()
    with db:  # transaction: fact update + event insert commit or roll back together
        db.execute(
            "INSERT INTO facts (key, value, source_client, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "source_client=excluded.source_client, updated_at=excluded.updated_at",
            (key, value, cid, _now()),
        )
        _log(cid, "set", key, value)
    return f"set {key!r} (from {cid})"


@mcp.tool()
@_tool_guard
def memory_set(key: str, value: str, client_id: str | None = None) -> str:
    """Store a fact (last-write-wins). Overwrites any existing value for
    `key`; the write is attributed to `client_id` (defaults to this
    server's MCP_CLIENT_ID env setting) and logged to events."""
    return _guarded_set(key, value, client_id)


@mcp.tool()
@_tool_guard
def memory_get(key: str, include_events: bool = False) -> str:
    """Fetch a fact by exact key. With include_events=True, appends the
    key's full write history from the events log."""
    row = _db().execute("SELECT value, source_client, updated_at FROM facts WHERE key = ?", (key,)).fetchone()
    _log(DEFAULT_CLIENT_ID, "get", key, None)  # reads are logged too, attributed to this server's client
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
@_tool_guard
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
@_tool_guard
def memory_delete(key: str, client_id: str | None = None) -> str:
    """Delete a fact by key. No-op if the key doesn't exist (still logged)."""
    cid = client_id or DEFAULT_CLIENT_ID
    db = _db()
    with db:
        cur = db.execute("DELETE FROM facts WHERE key = ?", (key,))
        _log(cid, "delete", key, None)
    if cur.rowcount == 0:
        return f"nothing to delete for {key!r} (logged from {cid})"
    return f"deleted {key!r} (from {cid})"


# Demo-only tool for the Phase 3 lock experiment: holds the SQLite write
# lock open for N seconds, so another client's write deterministically
# collides with it. Enable with MCP_ENABLE_DEMO_TOOLS=1; not registered
# otherwise so the production surface stays exactly the four memory tools.
if os.environ.get("MCP_ENABLE_DEMO_TOOLS") == "1":

    @mcp.tool()
    @_tool_guard
    def lock_hold(seconds: float = 2.0) -> str:
        """[demo] Hold the SQLite write lock for `seconds`, then commit."""
        db = _db()
        db.execute("BEGIN IMMEDIATE")  # grab the write lock and keep it
        db.execute(
            "INSERT INTO events (timestamp, client_id, action, key, value) VALUES (?, ?, 'demo-lock-hold', NULL, NULL)",
            (_now(), DEFAULT_CLIENT_ID),
        )
        import time

        time.sleep(seconds)
        db.commit()  # releases the lock
        return f"held the write lock for {seconds}s (client {DEFAULT_CLIENT_ID!r})"


if __name__ == "__main__":
    # Avoid stray print()s: stdout is the protocol channel.
    print(
        f"memory server: db={os.path.abspath(DB_PATH)} busy_timeout={BUSY_TIMEOUT_MS}ms "
        f"client_id={DEFAULT_CLIENT_ID!r}",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")
