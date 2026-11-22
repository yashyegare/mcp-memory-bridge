"""Pagination, input caps, read attribution, credential caching, and the
raw client's per-request deadline."""

import hashlib
import json
import os
import sys
import time

import pytest
from conftest import SERVER_CMD
from raw_client import RawMCPClient


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def _memory_client(make_client, tmp_path, **env):
    client = make_client(
        [*SERVER_CMD, str(tmp_path / "memory.db")],
        env={"MCP_EMBEDDINGS": "off", **env},
    )
    client.initialize()
    return client


def test_memory_list_pages_with_after_cursor(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    for i in range(5):
        client.call_tool("memory_set", {"key": f"p/{i}", "value": str(i)})

    first = _text(client.call_tool("memory_list", {"prefix": "p/", "limit": 2}))
    assert first.splitlines()[:2] == ["p/0 = 0  (source: anonymous)", "p/1 = 1  (source: anonymous)"]
    assert "pass after='p/1'" in first

    second = _text(client.call_tool("memory_list", {"prefix": "p/", "limit": 2, "after": "p/1"}))
    assert "p/2 = 2" in second and "p/3 = 3" in second and "p/1 =" not in second

    last = _text(client.call_tool("memory_list", {"prefix": "p/", "limit": 2, "after": "p/3"}))
    assert last == "p/4 = 4  (source: anonymous)"  # no "more results" line on the final page


def test_page_size_is_bounded(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path, MCP_MAX_PAGE_SIZE="10")
    result = client.call_tool("memory_list", {"limit": 11})
    assert result.get("isError") is True
    assert "limit must be 1..10" in _text(result)


def test_memory_history_returns_latest_events_in_order(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    for i in range(4):
        client.call_tool("memory_set", {"key": "h", "value": f"v{i}"})

    lines = _text(client.call_tool("memory_history", {"key": "h", "limit": 2})).splitlines()
    assert "latest 2 events" in lines[0]
    assert lines[1].endswith("v2") and lines[2].endswith("v3")

    full = _text(client.call_tool("memory_history", {"key": "h"}))
    assert "latest" not in full and full.count("set: v") == 4


def test_memory_get_attributes_reads_to_supplied_client_id(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "who", "value": "x", "client_id": "writer"})
    client.call_tool("memory_get", {"key": "who", "client_id": "reader"})
    history = _text(client.call_tool("memory_history", {"key": "who", "include_reads": True}))
    # history lines are "<timestamp> <client_id> <action>: <value>"
    assert any(line.split()[1:3] == ["reader", "get:"] for line in history.splitlines())


def test_search_query_length_is_capped_before_the_model_loads(make_client, tmp_path):
    # Embeddings enabled, but the cap rejects the query before any model
    # import, so this runs without sentence-transformers installed.
    client = _memory_client(make_client, tmp_path, MCP_EMBEDDINGS="1", MCP_MAX_QUERY_LEN="8")
    result = client.call_tool("memory_search", {"query": "x" * 9})
    assert result.get("isError") is True
    assert "query must be 1..8 characters" in _text(result)


def test_credentials_reparse_only_when_file_changes(tmp_path, monkeypatch):
    from mcp_memory_bridge import http_transport

    token = "a" * 40
    path = tmp_path / "clients.json"
    entry = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "scopes": ["read"]}
    path.write_text(json.dumps({"alice": entry}))
    old = time.time() - 60
    os.utime(path, (old, old))  # settled: older than the racy-timestamp window

    credentials = http_transport.Credentials(str(path), "")
    parses = []
    original = credentials._parse
    monkeypatch.setattr(credentials, "_parse", lambda: parses.append(1) or original())

    for _ in range(3):
        assert credentials.authenticate([f"Bearer {token}".encode()]) == ("alice", ["read"])
    assert parses == []  # served from cache

    path.write_text("{}")  # revoke
    assert credentials.authenticate([f"Bearer {token}".encode()]) is None
    assert parses == [1]

    path.write_text("not json")
    with pytest.raises(ValueError):
        credentials.authenticate([f"Bearer {token}".encode()])  # still fails closed


def test_notifications_do_not_extend_the_request_deadline():
    # A server that never answers but emits a notification every 0.3s. With
    # a per-message timeout the client would wait forever; with a request
    # deadline it gives up after ~1.5s.
    chatty = (
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "while True:\n"
        "    sys.stdout.write('{\"jsonrpc\":\"2.0\",\"method\":\"notifications/ping\"}\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.3)\n"
    )
    client = RawMCPClient([sys.executable, "-c", chatty], default_timeout=1.5)
    start = time.monotonic()
    try:
        with pytest.raises(ConnectionError):
            client._request("tools/list")
    finally:
        client.close()
    assert time.monotonic() - start < 5
