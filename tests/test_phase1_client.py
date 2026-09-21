"""Phase 1 tests: the hand-rolled client against the SDK test server."""

import sys

import pytest
from conftest import ROOT
from raw_client import MCPError, RawMCPClient

TEST_SERVER = str(ROOT / "server" / "test_server.py")


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def test_initialize_returns_capabilities(make_client):
    client = make_client([sys.executable, TEST_SERVER])
    caps = client.initialize()
    assert caps["protocolVersion"] == "2025-11-25"
    assert "tools" in caps["capabilities"]
    assert caps["serverInfo"]["name"] == "scratch-test-server"


def test_list_tools_returns_both_tools(make_client):
    client = make_client([sys.executable, TEST_SERVER])
    client.initialize()
    names = [t["name"] for t in client.list_tools()]
    assert names == ["echo", "add"]


def test_echo_round_trip(make_client):
    client = make_client([sys.executable, TEST_SERVER])
    client.initialize()
    result = client.call_tool("echo", {"text": "ping"})
    assert result.get("isError") in (None, False)
    assert "echo: ping" in _text(result)


def test_add_round_trip(make_client):
    client = make_client([sys.executable, TEST_SERVER])
    client.initialize()
    result = client.call_tool("add", {"a": 20, "b": 22})
    assert _text(result) == "42"


def test_unknown_tool_reports_an_error(make_client):
    """Both failure shapes are spec-legal; this SDK picks tool-level
    (isError=true on a successful response). Accept either, but require
    *an* error."""
    client = make_client([sys.executable, TEST_SERVER])
    client.initialize()
    try:
        result = client.call_tool("does_not_exist", {})
    except MCPError:
        return  # protocol-level shape: fine
    assert result.get("isError") is True
    assert _text(result)  # some human-readable message accompanies it


def test_hung_server_times_out_instead_of_hanging():
    """A server that never replies must surface as ConnectionError within
    the configured timeout, not block the client forever. (The child ignores
    stdin entirely and just sleeps -- reading one line and exiting would
    produce EOF, which is a different failure.)"""
    import time

    silent_server = [sys.executable, "-c", "import time; time.sleep(60)"]
    client = RawMCPClient(silent_server, default_timeout=2.0)
    try:
        # list_tools() uses default_timeout; initialize() deliberately allows
        # a longer 15s startup window for server import cost.
        start = time.monotonic()
        with pytest.raises(ConnectionError, match="within"):
            client.list_tools()
        elapsed = time.monotonic() - start
        assert elapsed < 10  # actually bounded, not 60s+
    finally:
        client.close()
