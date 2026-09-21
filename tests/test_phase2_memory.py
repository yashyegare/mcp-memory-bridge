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


def test_cas_succeeds_when_expected_matches(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "k", "value": "v1", "client_id": "a"})
    result = client.call_tool(
        "memory_set", {"key": "k", "value": "v2", "client_id": "a", "expected_value": "v1"}
    )
    assert result.get("isError") is not True
    assert "v2" in _text(client.call_tool("memory_get", {"key": "k"}))


def test_cas_rejects_when_expected_does_not_match(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "k", "value": "real", "client_id": "a"})
    result = client.call_tool(
        "memory_set",
        {"key": "k", "value": "hijack", "client_id": "b", "expected_value": "stale-guess"},
    )
    assert result.get("isError") is True
    assert "CAS failed" in _text(result)
    assert "stale-guess" in _text(result) and "real" in _text(result)
    # The rejected write must not have landed.
    assert "real" in _text(client.call_tool("memory_get", {"key": "k"}))


def test_cas_failure_is_logged_and_visible_in_history(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "k", "value": "real", "client_id": "a"})
    client.call_tool(
        "memory_set",
        {"key": "k", "value": "hijack", "client_id": "b", "expected_value": "stale-guess"},
    )
    history = _text(client.call_tool("memory_history", {"key": "k"}))
    assert "cas_fail" in history and "b" in history


def test_require_absent_succeeds_on_new_key(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    result = client.call_tool(
        "memory_set", {"key": "new-key", "value": "v1", "client_id": "a", "require_absent": True}
    )
    assert result.get("isError") is not True
    assert "v1" in _text(client.call_tool("memory_get", {"key": "new-key"}))


def test_require_absent_rejects_existing_key(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "k", "value": "original", "client_id": "a"})
    result = client.call_tool(
        "memory_set", {"key": "k", "value": "overwrite", "client_id": "b", "require_absent": True}
    )
    assert result.get("isError") is True
    assert "already exists" in _text(result)
    assert "original" in _text(client.call_tool("memory_get", {"key": "k"}))


def test_expected_value_and_require_absent_are_mutually_exclusive(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    result = client.call_tool(
        "memory_set",
        {"key": "k", "value": "v", "expected_value": "x", "require_absent": True},
    )
    assert result.get("isError") is True
    assert "mutually exclusive" in _text(result)


def test_get_missing_key_is_tool_level_error(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    result = client.call_tool("memory_get", {"key": "nope"})
    assert result.get("isError") is True
    assert "no fact for key" in _text(result)


def test_input_size_caps(make_client, tmp_path):
    """Keys/values are length-capped: a shared TEXT store with no limits is
    one memory_set away from a bloated database."""
    client = _memory_client(make_client, tmp_path)
    too_long_value = "x" * 16385
    result = client.call_tool("memory_set", {"key": "ok", "value": too_long_value})
    assert result.get("isError") is True
    assert "16384" in _text(result)
    too_long_key = "k" * 257
    result = client.call_tool("memory_set", {"key": too_long_key, "value": "v"})
    assert result.get("isError") is True
    assert "256" in _text(result)
    # The rejected writes must not have touched the store.
    listed = _text(client.call_tool("memory_list", {}))
    assert "ok" not in listed


def test_log_reads_off_makes_get_truly_read_only(make_client, tmp_path):
    """Default memory_get logs an event (a WRITE: takes the write lock, can
    queue behind another client). MCP_LOG_READS=off must (a) make get
    succeed even while another process holds the lock at busy_timeout=0,
    and (b) leave zero 'get' rows in the events table."""
    db_path = str(tmp_path / "memory.db")
    writer = make_client(
        [sys.executable, MEMORY_SERVER, db_path],
        env={"MCP_EMBEDDINGS": "off", "MCP_ENABLE_DEMO_TOOLS": "1"},
    )
    writer.initialize()
    writer.call_tool("memory_set", {"key": "r", "value": "v"})

    reader = make_client(
        [sys.executable, MEMORY_SERVER, db_path],
        env={"MCP_EMBEDDINGS": "off", "MCP_LOG_READS": "off", "MCP_SQLITE_BUSY_TIMEOUT": "0"},
    )
    reader.initialize()
    # 3s < lock_hold's default 2s + overhead, so the get lands mid-hold.
    import threading

    def read_during_hold():
        import time

        time.sleep(1.0)
        result = reader.call_tool("memory_get", {"key": "r"})
        read_during_hold.result_text = _text(result)
        read_during_hold.is_error = result.get("isError") is True

    read_during_hold.result_text = None
    read_during_hold.is_error = True
    t = threading.Thread(target=read_during_hold)
    t.start()
    writer.call_tool("lock_hold", {"seconds": 2.0})
    t.join(timeout=20.0)
    assert not t.is_alive(), "reader thread hung behind the write lock"
    assert read_during_hold.is_error is False, read_during_hold.result_text
    assert "r = v" in read_during_hold.result_text

    import sqlite3 as s3

    conn = s3.connect(db_path)
    try:
        n_gets = conn.execute("SELECT COUNT(*) FROM events WHERE action='get'").fetchone()[0]
    finally:
        conn.close()
    assert n_gets == 0, "MCP_LOG_READS=off still logged reads"


def test_log_reads_default_records_get_events(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "r", "value": "v"})
    client.call_tool("memory_get", {"key": "r"})
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    try:
        n_gets = conn.execute("SELECT COUNT(*) FROM events WHERE action='get'").fetchone()[0]
    finally:
        conn.close()
    assert n_gets >= 1, "default config should log get events"


def test_list_by_prefix(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    for key, value in [("a/one", "1"), ("a/two", "2"), ("b/three", "3")]:
        client.call_tool("memory_set", {"key": key, "value": value})
    listed = _text(client.call_tool("memory_list", {"prefix": "a/"}))
    assert "a/one" in listed and "a/two" in listed and "b/three" not in listed


def test_list_prefix_escapes_like_wildcards(make_client, tmp_path):
    """Regression: a prefix containing % or _ must match LITERALLY.
    SQLite's LIKE has no default escape character, so the server's backslash
    escaping does nothing without an ESCAPE clause -- 50%_off used to match
    '50xooff', '50Xooff', etc. (and 5% alone matched nearly everything)."""
    client = _memory_client(make_client, tmp_path)
    for key in ("50%_off", "50xoff", "50%foo", "other-key"):
        client.call_tool("memory_set", {"key": key, "value": "v"})

    listed = _text(client.call_tool("memory_list", {"prefix": "50%_"}))
    assert "50%_off" in listed
    assert "50xoff" not in listed and "50%foo" not in listed and "other-key" not in listed

    listed_all = _text(client.call_tool("memory_list", {"prefix": ""}))
    assert "50%_off" in listed_all and "other-key" in listed_all  # empty prefix unaffected


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
    assert next(line for line in event_lines if "cursor" in line).startswith("  ")
    assert event_lines.index(
        next(line for line in event_lines if "cursor said A" in line)
    ) < event_lines.index(next(line for line in event_lines if "claude said B" in line))


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
