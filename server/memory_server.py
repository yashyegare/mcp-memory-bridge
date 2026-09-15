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

import numpy as np  # embeddings math (Phase 4A); trivial install, no heavy deps

# mcp 2.x note: FastMCP was renamed to MCPServer (mcp 1.x import fails).
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# Config via argv > env > defaults, so Claude Desktop / any launcher can
# configure this server without touching code.
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_MEMORY_DB", "memory.db")
BUSY_TIMEOUT_MS = int(os.environ.get("MCP_SQLITE_BUSY_TIMEOUT", "5000"))
DEFAULT_CLIENT_ID = os.environ.get("MCP_CLIENT_ID", "anonymous")
# Phase 4A: set MCP_EMBEDDINGS=off for a lean server with no ML dependencies.
EMBEDDINGS_ENABLED = os.environ.get("MCP_EMBEDDINGS", "1") != "off"

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
                action    TEXT NOT NULL,   -- 'set' | 'get' | 'delete' | 'search'
                key       TEXT,
                value     TEXT
            );

            CREATE TABLE IF NOT EXISTS fact_embeddings (
                key       TEXT PRIMARY KEY,   -- mirrors facts.key
                embedding BLOB NOT NULL       -- normalized float32 vector (384 dims)
            );
            """
        )
        _conn.commit()
    return _conn


# ---------------------------------------------------------------------- #
# Phase 4A: semantic recall                                              #
# ---------------------------------------------------------------------- #
# all-MiniLM-L6-v2, 384-dim embeddings, ~80MB, CPU-friendly. The model is
# loaded LAZILY (first embed call) and kept as a singleton: loading at
# server start would tax every client spawn (~10s import + ~16s model load
# + ~500MB RAM), and each MCP client spawns its own server subprocess.
# With the cache warm, first embed is a few seconds and subsequent ones
# are milliseconds -- never per-call loading, per the design note.
_model = None


def _embed(texts: list[str]):
    """Embed texts as normalized float32 numpy vectors (cosine-ready)."""
    global _model
    if _model is None:
        if not EMBEDDINGS_ENABLED:
            raise ToolError(
                "semantic search is disabled on this server (MCP_EMBEDDINGS=off); "
                "use memory_list with a key prefix instead"
            )
        # Offline mode unless overridden: the model is cached after the first
        # run, and letting HF Hub do a network freshness check on load risks
        # hanging the first tool call on a flaky network (observed here).
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer  # deferred heavy import

        _model = SentenceTransformer("all-MiniLM-L6-v2")
    vectors = _model.encode(texts, normalize_embeddings=True)
    return vectors.astype("float32")


def _remember_search(query: str, matched_key: str, score: float) -> None:
    """Log a search event with the query and its best match (audit trail
    for semantic recall: what was asked, in which words, and what won)."""
    db = _db()
    with db:
        db.execute(
            "INSERT INTO events (timestamp, client_id, action, key, value) VALUES (?, ?, 'search', ?, ?)",
            (_now(), DEFAULT_CLIENT_ID, matched_key, f"query={query!r} score={score:.3f}"),
        )


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
    # Embed BEFORE opening the write transaction: the first call loads the
    # model (~seconds) and must not hold SQLite's write lock while doing it.
    embedding = _embed([f"{key}: {value}"])[0].tobytes() if EMBEDDINGS_ENABLED else None
    with db:  # transaction: fact update + event insert (+ embedding) commit together
        db.execute(
            "INSERT INTO facts (key, value, source_client, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "source_client=excluded.source_client, updated_at=excluded.updated_at",
            (key, value, cid, _now()),
        )
        if embedding is not None:
            db.execute(
                "INSERT INTO fact_embeddings (key, embedding) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET embedding=excluded.embedding",
                (key, embedding),
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
def memory_search(query: str, top_k: int = 3) -> str:
    """Find facts by MEANING, not key: embeds the query and returns the
    top_k most similar facts by cosine similarity. Works with different
    words than were stored (e.g. query 'appearance preference' finds
    key 'user/theme')."""
    if not EMBEDDINGS_ENABLED:
        raise ToolError(
            "semantic search is disabled on this server (MCP_EMBEDDINGS=off); "
            "use memory_list with a key prefix instead"
        )
    rows = _db().execute(
        "SELECT f.key, f.value, f.source_client, f.updated_at, e.embedding "
        "FROM facts f JOIN fact_embeddings e ON e.key = f.key"
    ).fetchall()
    if not rows:
        return "(no searchable facts stored yet)"
    query_vec = _embed([query])[0]
    scored = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype="float32")
        score = float(np.dot(query_vec, vec))  # both normalized: dot == cosine
        scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[: max(1, top_k)]
    best_key, best_score = top[0][1]["key"], top[0][0]
    _remember_search(query, best_key, best_score)
    lines = [
        f"{row['key']} = {row['value']}  (cosine {score:.3f}, source: {row['source_client']})"
        for score, row in top
    ]
    return "\n".join(lines)


@mcp.tool()
@_tool_guard
def memory_history(key: str, include_reads: bool = False) -> str:
    """Full audit trail for one key: every set/delete event in order, with
    attribution -- who wrote what, when. include_reads=True also shows
    every get and every semantic-search hit on this key (verbose)."""
    actions = "('set', 'get', 'delete', 'search')" if include_reads else "('set', 'delete')"
    rows = _db().execute(
        f"SELECT timestamp, client_id, action, value FROM events "
        f"WHERE key = ? AND action IN {actions} ORDER BY id",
        (key,),
    ).fetchall()
    if not rows:
        return f"(no recorded history for key {key!r})"
    return "\n".join(f"{r['timestamp']} {r['client_id']:>14} {r['action']:>6}: {r['value']}" for r in rows)


@mcp.tool()
@_tool_guard
def memory_delete(key: str, client_id: str | None = None) -> str:
    """Delete a fact by key. No-op if the key doesn't exist (still logged)."""
    cid = client_id or DEFAULT_CLIENT_ID
    db = _db()
    with db:
        cur = db.execute("DELETE FROM facts WHERE key = ?", (key,))
        db.execute("DELETE FROM fact_embeddings WHERE key = ?", (key,))  # keep vectors in sync
        _log(cid, "delete", key, None)
    if cur.rowcount == 0:
        return f"nothing to delete for {key!r} (logged from {cid})"
    return f"deleted {key!r} (from {cid})"


# Demo-only tool for the Phase 3 lock experiment: holds the SQLite write
# lock open for N seconds, so another client's write deterministically
# collides with it. Enable with MCP_ENABLE_DEMO_TOOLS=1; not registered
# otherwise so the production tool surface stays the memory tools only.
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


def _maybe_preload_model() -> None:
    """With MCP_PRELOAD_MODEL=1, load the embedding model in a background
    thread at startup: the server still starts instantly, but the model is
    (usually) ready by the first real tool call, so clients never see the
    ~25s cold load on their first memory_set."""
    if not (EMBEDDINGS_ENABLED and os.environ.get("MCP_PRELOAD_MODEL") == "1"):
        return

    def _load():
        try:
            _embed(["preload"])
            print("embedding model preloaded", file=sys.stderr)
        except Exception as exc:  # preload is best-effort; lazy load still works
            print(f"embedding model preload failed: {exc}", file=sys.stderr)

    threading.Thread(target=_load, daemon=True).start()


if __name__ == "__main__":
    # Avoid stray print()s: stdout is the protocol channel.
    print(
        f"memory server: db={os.path.abspath(DB_PATH)} busy_timeout={BUSY_TIMEOUT_MS}ms "
        f"client_id={DEFAULT_CLIENT_ID!r}",
        file=sys.stderr,
    )
    _maybe_preload_model()
    mcp.run(transport="stdio")
