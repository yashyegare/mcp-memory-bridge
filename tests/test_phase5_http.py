"""Phase 4C tests: the streamable-HTTP transport, client and server.

Same discipline as the rest of the suite -- real server process, real wire
protocol -- but over HTTP: one JSON body per request, bearer-token auth in
front of the MCP layer, and a server-issued Mcp-Session-Id the client must
echo. No embedding model here (MCP_EMBEDDINGS=off): these tests cover
transport and auth, not semantics.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from conftest import ROOT
from raw_client import MCPError, RawHTTPMCPClient

MEMORY_SERVER = str(ROOT / "server" / "memory_server.py")
TOKEN = "test-token-0123456789"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    """One HTTP server for the whole module: boot is ~3s (uvicorn), the
    tests share one db file, which is the point of the transport."""
    port = _free_port()
    db = tmp_path_factory.mktemp("http") / "memory.db"
    env = {
        **os.environ,
        "MCP_TRANSPORT": "http",
        "MCP_AUTH_TOKEN": TOKEN,
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_PORT": str(port),
        "MCP_EMBEDDINGS": "off",
    }
    proc = subprocess.Popen(
        [sys.executable, MEMORY_SERVER, str(db)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    # Wait for the socket to accept (uvicorn boot), then give the app a beat.
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError("http server exited during boot")
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("http server never opened its port")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture()
def http_client(http_server):
    client = RawHTTPMCPClient(http_server, token=TOKEN)
    client.initialize()
    yield client
    client.close()


def test_initialize_handshake_and_session(http_client):
    """Handshake works over HTTP and the server issued a session id."""
    assert http_client.session_id  # captured from Mcp-Session-Id
    tools = http_client.list_tools()
    assert "memory_set" in [t["name"] for t in tools]


def test_set_get_round_trip_over_http(http_client):
    http_client.call_tool("memory_set", {"key": "http/rt", "value": "wire value", "client_id": "http-test"})
    text = "\n".join(
        b.get("text", "") for b in http_client.call_tool("memory_get", {"key": "http/rt"})["content"]
    )
    assert "http/rt = wire value" in text
    assert "source: http-test" in text


def test_missing_token_rejected_as_401(http_server):
    """No Authorization header -> 401 with WWW-Authenticate, before any MCP
    work happens. Response body is a JSON-RPC error object so even a raw
    socket client can interpret the failure."""
    req = urllib.request.Request(http_server, data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    }).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req)
        pytest.fail("request without a token was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
        assert exc.headers.get("WWW-Authenticate") == "Bearer"
        body = json.loads(exc.read().decode())
        assert body["error"]["code"] == -32001


def test_wrong_token_rejected_as_401(http_server):
    client = RawHTTPMCPClient(http_server, token="definitely-wrong")
    with pytest.raises(MCPError, match="401"):
        client.initialize()


def test_unknown_tool_is_tool_level_error_over_http(http_client):
    """Layering check: an unknown tool is a 200 whose result carries
    isError=true -- a tool-level failure inside a successful HTTP request,
    the mirror image of the 401 case where the request dies before JSON-RPC
    exists. (Same shape the SDK returns over stdio.)"""
    result = http_client.call_tool("does_not_exist", {})
    assert result.get("isError") is True
    assert "Unknown tool" in "\n".join(b.get("text", "") for b in result["content"])


def test_two_clients_share_the_store(http_server):
    """Two independent HTTP clients (separate sessions) see each other's
    writes -- the transport-level version of the two-subprocess Phase 2 test."""
    a = RawHTTPMCPClient(http_server, token=TOKEN)
    b = RawHTTPMCPClient(http_server, token=TOKEN)
    a.initialize()
    b.initialize()
    try:
        a.call_tool("memory_set", {"key": "shared/via-http", "value": "from a", "client_id": "client-a"})
        text = "\n".join(
            blk.get("text", "") for blk in b.call_tool("memory_get", {"key": "shared/via-http"})["content"]
        )
        assert "from a" in text
    finally:
        a.close()
        b.close()


def test_http_server_refuses_to_start_without_token(tmp_path):
    """Fail-closed: MCP_TRANSPORT=http without MCP_AUTH_TOKEN must exit
    nonzero with an explanatory message, never serve unauthenticated."""
    env = {**os.environ, "MCP_TRANSPORT": "http", "MCP_AUTH_TOKEN": "",
           "MCP_EMBEDDINGS": "off"}
    proc = subprocess.Popen(
        [sys.executable, MEMORY_SERVER, str(tmp_path / "m.db")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
    )
    try:
        _, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 2
    assert b"MCP_AUTH_TOKEN" in stderr
