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
  history, not early rejection by default. The events table provides
  both, and callers who DO want to reject on conflict opt in per-call:
  `memory_set(..., expected_value=...)` performs compare-and-swap, and
  `memory_set(..., require_absent=True)` is create-only. Both are a
  single atomic SQL statement (conditional UPDATE / bare INSERT relying
  on the PK conflict), not a Python-level read-then-write -- a
  read-then-write would race exactly the way _SERVER_LOCK exists to
  prevent, just one layer up. A failed CAS attempt is still logged
  (action='cas_fail', see memory_history) so "who tried to overwrite
  this and lost" is as debuggable as an accepted write.

Writes are attributed: every fact row carries source_client, and every
mutation appends to `events`. `busy_timeout` is set high (5s) so
near-simultaneous writers from different clients queue rather than
fail with "database is locked". WAL mode matters ACROSS server
processes (one per MCP client): readers there never block on another
process's write transaction. Within one process the server-wide lock
serializes database access. Write embeddings compute separately so reads
can proceed while a writer prepares its vector.

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

import audit
import embeddings
import http_transport
import numpy as np  # embeddings math (Phase 4A); trivial install, no heavy deps
import storage

# mcp 2.x note: FastMCP was renamed to MCPServer (mcp 1.x import fails).
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from telemetry import METRICS, debug_event

# Config via argv > env > defaults, so Claude Desktop / any launcher can
# configure this server without touching code.
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_MEMORY_DB", "memory.db")
BUSY_TIMEOUT_MS = int(os.environ.get("MCP_SQLITE_BUSY_TIMEOUT", "5000"))
SYNCHRONOUS = os.environ.get("MCP_SQLITE_SYNCHRONOUS", "NORMAL").upper()
DEFAULT_CLIENT_ID = os.environ.get("MCP_CLIENT_ID", "anonymous")
# Phase 4A: set MCP_EMBEDDINGS=off for a lean server with no ML dependencies.
EMBEDDINGS_ENABLED = os.environ.get("MCP_EMBEDDINGS", "1") != "off"
# Input limits: keys/values are TEXT in a shared store and nothing else
# bounds them, so without caps a single caller could bloat the db with a
# multi-MB value. Sized for facts, not documents (env-overridable).
MAX_KEY_LEN = int(os.environ.get("MCP_MAX_KEY_LEN", "256"))
MAX_VALUE_LEN = int(os.environ.get("MCP_MAX_VALUE_LEN", "16384"))
# Read logging: by default every memory_get appends an events row -- which
# makes each read a WRITE (write lock + WAL frames). Set MCP_LOG_READS=off
# for a truly read-only memory_get (it then completes even while another
# process holds the write lock, even at busy_timeout=0), at the cost of
# losing read attribution in the audit trail.
LOG_READS = os.environ.get("MCP_LOG_READS", "1") != "off"
# Phase 4C: HTTP transport. stdio stays the default (one server subprocess
# per client); MCP_TRANSPORT=http serves streamable-HTTP instead, so remote
# clients can share ONE server (and one store) over the network. The
# trust model behind the token requirement lives in README.md, Phase 4C.
TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
AUTH_CLIENTS_FILE = os.environ.get("MCP_AUTH_CLIENTS_FILE", "")
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


def _validate(key: str, value: str | None = None) -> None:
    """Enforce input size caps. ToolError so the message survives to the
    client (mcp 2.x flattens arbitrary exceptions -- see _tool_guard)."""
    if not key or len(key) > MAX_KEY_LEN:
        raise ToolError(f"key must be 1..{MAX_KEY_LEN} characters (got {len(key)})")
    if value is not None and len(value) > MAX_VALUE_LEN:
        raise ToolError(
            f"value exceeds {MAX_VALUE_LEN} characters (got {len(value)}); "
            "this store is for facts, not documents"
        )


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # busy_timeout: how long a write waits for the lock before raising
        # "database is locked". 0ms = fail fast (for experiments), default
        # 5s = queue concurrent writers (the production setting).
        conn = sqlite3.connect(
            DB_PATH,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,  # SDK dispatches calls across threads; _SERVER_LOCK serializes
        )
        try:
            storage.initialize(conn, SYNCHRONOUS)
        except BaseException:
            conn.close()
            raise
        _conn = conn
    return _conn


def _client_id(ctx: Context, claimed: str | None = None, write: bool = False) -> str:
    if TRANSPORT == "http" and AUTH_CLIENTS_FILE:
        request = ctx.request_context.request
        user = request.scope.get("user") if request is not None else None
        if not isinstance(user, AuthenticatedUser):
            raise ToolError("authenticated client identity unavailable")
        required = "write" if write else "read"
        if required not in user.scopes:
            raise ToolError(f"credential requires {required} scope")
        return user.access_token.client_id
    return claimed or DEFAULT_CLIENT_ID


def _log(client_id: str, action: str, key: str | None, value: str | None,
         timestamp: str | None = None, embedding: bytes | None = None) -> None:
    audit.append(_db(), timestamp or _now(), client_id, action, key, value, embedding)


def _tool_guard(fn):
    """Record full tool latency and preserve actionable SQLite errors."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        error = None
        try:
            ctx = kwargs.get("ctx")
            if ctx is not None:
                _client_id(ctx)
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            error = exc
            if "locked" in str(exc) or "busy" in str(exc):
                raise ToolError(
                    f"database is locked (busy_timeout={BUSY_TIMEOUT_MS}ms): {exc}. "
                    "Another client holds the write lock; retry with backoff."
                ) from None
            raise
        except BaseException as exc:
            error = exc
            raise
        finally:
            METRICS.observe("tool", time.perf_counter() - start)
            if error:
                METRICS.add("tool_failures_total")
            debug_event(fn.__name__, start, error)
    return wrapper


def _db_guard(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        with _SERVER_LOCK:
            METRICS.observe("lock_wait", time.perf_counter() - start)
            return fn(*args, **kwargs)
    return wrapper


@_db_guard
def _guarded_set(
    key: str,
    value: str,
    cid: str,
    embedding: bytes | None,
    expected_value: str | None = None,
    require_absent: bool = False,
) -> str:
    if require_absent and expected_value is not None:
        raise ToolError("require_absent and expected_value are mutually exclusive")
    _validate(key, value)
    db = _db()

    timestamp = _now()
    cas_failure: str | None = None
    with db:  # transaction: fact update + event insert (+ embedding) commit together
        if require_absent:
            # Atomic create-only: the PK conflict IS the failure signal.
            # A "check it doesn't exist, then INSERT" would race under
            # concurrent writers -- this can't, since it's one statement.
            try:
                db.execute(
                    "INSERT INTO facts (key, value, source_client, updated_at) VALUES (?, ?, ?, ?)",
                    (key, value, cid, timestamp),
                )
            except sqlite3.IntegrityError:
                current = db.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
                cas_failure = f"key already exists (current value: {current['value'] if current else '<unknown>'})"
        elif expected_value is not None:
            # Compare-and-swap as ONE conditional UPDATE: the match check
            # and the write happen in the same statement, so there's no
            # window between "read current value" and "write new value"
            # for another writer to land in. A Python-level read-then-
            # write here would reintroduce the exact race _SERVER_LOCK
            # exists to prevent, one layer up, and only within this
            # process -- it wouldn't protect against another server
            # process (another MCP client) writing between the two steps.
            cur = db.execute(
                "UPDATE facts SET value=?, source_client=?, updated_at=? "
                "WHERE key=? AND value=?",
                (value, cid, timestamp, key, expected_value),
            )
            if cur.rowcount == 0:
                current = db.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
                actual = current["value"] if current else None
                cas_failure = f"expected {expected_value!r}, actual is {actual!r}"
        else:
            db.execute(
                "INSERT INTO facts (key, value, source_client, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "source_client=excluded.source_client, updated_at=excluded.updated_at",
                (key, value, cid, timestamp),
            )

        if cas_failure is None:
            if embedding is not None:
                db.execute(
                    "INSERT INTO fact_embeddings (key, embedding) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET embedding=excluded.embedding",
                    (key, embedding),
                )
            else:
                db.execute("DELETE FROM fact_embeddings WHERE key=?", (key,))
            _log(cid, "set", key, value, timestamp, embedding)
        else:
            _log(cid, "cas_fail", key, cas_failure)

    if cas_failure is not None:
        raise ToolError(f"CAS failed for {key!r}: {cas_failure}")
    return f"set {key!r} (from {cid})"


@mcp.tool()
@_tool_guard
def memory_set(
    key: str,
    value: str,
    ctx: Context,
    client_id: str | None = None,
    expected_value: str | None = None,
    require_absent: bool = False,
) -> str:
    """Store a fact. Default is last-write-wins (unconditional overwrite).

    For optimistic concurrency, pass expected_value: the write only
    commits if the key's CURRENT value equals expected_value exactly
    (compare-and-swap) -- otherwise it's rejected with the actual current
    value in the error, so the caller can re-read and retry instead of
    silently clobbering someone else's write.

    Pass require_absent=True instead to only succeed if the key does not
    exist yet (create-only). Mutually exclusive with expected_value.

    Every attempt is logged -- successful writes as 'set', failed CAS/
    create-only attempts as 'cas_fail' -- so see memory_history to debug
    who tried to write what, and who lost."""
    # Prepare the vector before entering the serialized database phase.
    cid = _client_id(ctx, client_id, write=True)
    _validate(key, value)
    if require_absent and expected_value is not None:
        raise ToolError("require_absent and expected_value are mutually exclusive")
    # Model work is serialized by its own lock and does not block DB reads.
    embedding = embeddings._embed([f"{key}: {value}"])[0].tobytes() if EMBEDDINGS_ENABLED else None
    return _guarded_set(key, value, cid, embedding, expected_value, require_absent)


@mcp.tool()
@_tool_guard
@_db_guard
def memory_get(key: str, ctx: Context, include_events: bool = False) -> str:
    """Fetch a fact by exact key. With include_events=True, appends the
    key's full write history from the events log."""
    _validate(key)
    row = _db().execute("SELECT value, source_client, updated_at FROM facts WHERE key = ?", (key,)).fetchone()
    if LOG_READS:
        # Reads are logged -- which makes them WRITES: they take the write
        # lock and can block behind another process's transaction (or fail
        # at busy_timeout=0). MCP_LOG_READS=off gives a genuinely read-only
        # get, giving up read attribution. See README design notes.
        with _db():
            _log(_client_id(ctx), "get", key, None)
    if row is None:
        # ToolError => isError=true with this message intact (mcp 2.x drops
        # the message of arbitrary exceptions; anticipated errors survive).
        raise ToolError(f"no fact for key {key!r}")
    out = f"{key} = {row['value']}  (source: {row['source_client']}, updated: {row['updated_at']})"
    if include_events:
        rows = _db().execute(
            "SELECT timestamp, client_id, action, value FROM events "
            "WHERE key = ? AND action IN ('set', 'delete', 'cas_fail') ORDER BY id",
            (key,),
        ).fetchall()
        out += "\nevents:\n" + "\n".join(
            f"  {r['timestamp']} {r['client_id']} {r['action']}: {r['value']}" for r in rows
        )
    return out


@mcp.tool()
@_tool_guard
@_db_guard
def memory_list(ctx: Context, prefix: str = "") -> str:
    """List facts whose key starts with `prefix` (all facts if empty)."""
    # Escape LIKE wildcards in the prefix, and tell SQLite which escape
    # character we used: without ESCAPE '\', "\%" is just backslash-percent
    # (a literal backslash followed by a wildcard), so a prefix containing
    # % or _ would silently match more than the caller asked for.
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = _db().execute(
        "SELECT key, value, source_client, updated_at FROM facts "
        "WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
        (escaped + "%",),
    ).fetchall()
    if not rows:
        return f"(no facts matching prefix {prefix!r})"
    return "\n".join(f"{r['key']} = {r['value']}  (source: {r['source_client']})" for r in rows)


@mcp.tool()
@_tool_guard
@_db_guard
def memory_search(query: str, ctx: Context, top_k: int = 3) -> str:
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
    query_vec = embeddings._embed([query])[0]
    scored = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype="float32")
        score = float(np.dot(query_vec, vec))  # both normalized: dot == cosine
        scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[: max(1, top_k)]
    best_key, best_score = top[0][1]["key"], top[0][0]
    with _db():
        _log(_client_id(ctx), "search", best_key, f"query={query!r} score={best_score:.3f}")
    lines = [
        f"{row['key']} = {row['value']}  (cosine {score:.3f}, source: {row['source_client']})"
        for score, row in top
    ]
    return "\n".join(lines)


@mcp.tool()
@_tool_guard
@_db_guard
def memory_history(key: str, ctx: Context, include_reads: bool = False) -> str:
    """Full audit trail for one key: every set/delete/cas_fail event in
    order, with attribution -- who wrote what (and who tried and lost a
    CAS/create-only race), when. include_reads=True also shows every get
    and every semantic-search hit on this key (verbose)."""
    actions = (
        "('set', 'get', 'delete', 'search', 'cas_fail')"
        if include_reads
        else "('set', 'delete', 'cas_fail')"
    )
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
@_db_guard
def memory_delete(key: str, ctx: Context, client_id: str | None = None) -> str:
    """Delete a fact by key. No-op if the key doesn't exist (still logged)."""
    cid = _client_id(ctx, client_id, write=True)
    _validate(key)
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
    @_db_guard
    def lock_hold(ctx: Context, seconds: float = 2.0) -> str:
        """[demo] Hold the SQLite write lock for `seconds`, then commit."""
        cid = _client_id(ctx, write=True)
        with _db():
            _log(cid, "demo-lock-hold", None, None)
            time.sleep(seconds)
        return f"held the write lock for {seconds}s (client {cid!r})"


def _operational_state() -> dict:
    with _SERVER_LOCK:
        db = _db()
        count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"chain_length": count, "sqlite_synchronous": db.execute("PRAGMA synchronous").fetchone()[0]}


if __name__ == "__main__":
    if SYNCHRONOUS not in ("NORMAL", "FULL"):
        print("MCP_SQLITE_SYNCHRONOUS must be NORMAL or FULL", file=sys.stderr)
        sys.exit(2)
    # Avoid stray print()s: stdout is the protocol channel (stdio mode).
    print(
        f"memory server: db={os.path.abspath(DB_PATH)} busy_timeout={BUSY_TIMEOUT_MS}ms "
        f"client_id={DEFAULT_CLIENT_ID!r} transport={TRANSPORT}",
        file=sys.stderr,
    )
    embeddings._maybe_preload_model()
    if TRANSPORT == "http":
        http_transport.run(mcp, _operational_state, METRICS)
    else:
        mcp.run(transport="stdio")
