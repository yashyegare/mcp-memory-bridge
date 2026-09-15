"""Phase 2 tests: the raw client against the SQLite memory server.

Each test gets a fresh temporary database, but the server under test is the
real one -- real subprocess, real wire protocol, real SQLite file."""

import sqlite3
import sys

from conftest import ROOT

MEMORY_SERVER = str(ROOT / "server" / "memory_server.py")


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def _memory_client(make_client, tmp_path):
    # MCP_EMBEDDINGS=off: these tests exercise storage/concurrency, not the
    # embedding model (loading it per test would add ~25s each).
    client = make_client(
        [sys.executable, MEMORY_SERVER, str(tmp_path / "memory.db")],
        env={"MCP_EMBEDDINGS": "off"},
    )
    client.initialize()
    return client


def test_set_get_round_trip(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "user/theme", "value": "dark", "client_id": "test"})
    result = client.call_tool("memory_get", {"key": "user/theme"})
    assert "user/theme = dark" in _text(result)
    assert "source: test" in _text(result)


def test_get_missing_key_is_tool_level_error(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    result = client.call_tool("memory_get", {"key": "nope"})
    assert result.get("isError") is True
    assert "no fact for key" in _text(result)


def test_list_by_prefix(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    for key, value in [("a/one", "1"), ("a/two", "2"), ("b/three", "3")]:
        client.call_tool("memory_set", {"key": key, "value": value})
    listed = _text(client.call_tool("memory_list", {"prefix": "a/"}))
    assert "a/one" in listed and "a/two" in listed and "b/three" not in listed


def test_delete_then_get_fails(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "x", "value": "1"})
    deleted = _text(client.call_tool("memory_delete", {"key": "x"}))
    assert "deleted" in deleted
    result = client.call_tool("memory_get", {"key": "x"})
    assert result.get("isError") is True


def test_last_write_wins_with_attribution_and_history(make_client, tmp_path):
    """The conflict policy, verified end to end: last writer wins, and the
    events log preserves who overwrote whom."""
    client = _memory_client(make_client, tmp_path)
    key = "shared/decision"
    client.call_tool("memory_set", {"key": key, "value": "cursor said A", "client_id": "cursor"})
    client.call_tool("memory_set", {"key": key, "value": "claude said B", "client_id": "claude"})

    result = _text(client.call_tool("memory_get", {"key": key, "include_events": True}))
    # LWW: the second write is the current value, attributed to its client.
    assert "claude said B" in result and "source: claude" in result
    # But the audit trail still shows both writes, in order -- so inspect the
    # events section specifically (the current value also mentions 'claude
    # said B' above it, so a plain index() would find the wrong occurrence).
    assert "events:" in result
    events_part = result.split("\nevents:\n", 1)[1]
    event_lines = [line for line in events_part.splitlines() if line.strip()]
    assert any("cursor set: cursor said A" in line for line in event_lines)
    assert any("claude set: claude said B" in line for line in event_lines)
    assert [line for line in event_lines if "cursor" in line][0].startswith("  ")
    assert event_lines.index(
        next(l for l in event_lines if "cursor said A" in l)
    ) < event_lines.index(next(l for l in event_lines if "claude said B" in l))


def test_database_is_in_wal_mode(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "k", "value": "v"})
    db = sqlite3.connect(str(tmp_path / "memory.db"))
    try:
        assert db.execute("PRAGMA journal_mode;").fetchone()[0] == "wal"
    finally:
        db.close()


def test_events_log_records_writes(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "k", "value": "v", "client_id": "tester"})
    db = sqlite3.connect(str(tmp_path / "memory.db"))
    try:
        rows = db.execute(
            "SELECT client_id, action, key, value FROM events WHERE action='set'"
        ).fetchall()
    finally:
        db.close()
    assert rows == [("tester", "set", "k", "v")]


def _contended_lock_scenario(make_client, tmp_path, extra_env):
    """Shared setup: two clients on one db; client B is inside lock_hold,
    actively holding the SQLite write lock, when this returns."""
    import threading
    import time

    env = {"MCP_EMBEDDINGS": "off", **extra_env}
    a = make_client(
        [sys.executable, MEMORY_SERVER, str(tmp_path / "memory.db")],
        env={**env, "MCP_CLIENT_ID": "A"},
    )
    b = make_client(
        [sys.executable, MEMORY_SERVER, str(tmp_path / "memory.db")],
        env={**env, "MCP_CLIENT_ID": "B"},
    )
    a.initialize()
    b.initialize()
    a.call_tool("memory_set", {"key": "warm", "value": "1"})  # create schema

    done = threading.Event()

    def run_b():
        b.call_tool("lock_hold", {"seconds": 1.0})
        done.set()

    t = threading.Thread(target=run_b)
    t.start()
    time.sleep(0.5)  # B is now inside its write transaction, holding the lock
    return a, b, t, done


def test_busy_timeout_zero_fails_fast_on_contended_lock(make_client, tmp_path):
    """The break, on demand: busy_timeout=0 + a held lock => the write comes
    back as a tool-level error whose MESSAGE survives (ToolError), unlike a
    raw sqlite3.OperationalError which mcp 2.x would flatten."""
    env = {"MCP_SQLITE_BUSY_TIMEOUT": "0", "MCP_ENABLE_DEMO_TOOLS": "1"}
    a, _b, t, done = _contended_lock_scenario(make_client, tmp_path, env)
    try:
        result = a.call_tool("memory_set", {"key": "x", "value": "y"})
        assert result.get("isError") is True, f"expected lock error, got: {result}"
        message = _text(result)
        assert "locked" in message.lower()
        # What matters is that the DETAIL survived. mcp 2.x prefixes even
        # ToolError messages with 'Error executing tool <name>: ', but keeps
        # the rest -- arbitrary exceptions would leave ONLY that prefix.
        assert "retry with backoff" in message
    finally:
        t.join()
        assert done.is_set()


def test_busy_timeout_default_queues_writers(make_client, tmp_path):
    """The fix: with the default 5s busy_timeout, a write that arrives while
    another client holds the lock simply waits and then succeeds."""
    import time

    env = {"MCP_ENABLE_DEMO_TOOLS": "1"}  # default MCP_SQLITE_BUSY_TIMEOUT=5000
    a, _b, t, done = _contended_lock_scenario(make_client, tmp_path, env)
    try:
        start = time.monotonic()
        result = a.call_tool("memory_set", {"key": "x", "value": "y"})
        elapsed = time.monotonic() - start
        assert result.get("isError") is not True
        assert elapsed >= 0.4  # it really did wait out the held lock
    finally:
        t.join()
        assert done.is_set()
