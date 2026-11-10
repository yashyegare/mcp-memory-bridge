"""Phase 4C tests: the streamable-HTTP transport, client and server.

Same discipline as the rest of the suite -- real server process, real wire
protocol -- but over HTTP: one JSON body per request, bearer-token auth in
front of the MCP layer, and a server-issued Mcp-Session-Id the client must
echo. No embedding model here (MCP_EMBEDDINGS=off): these tests cover
transport and auth, not semantics.
"""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest
from conftest import ROOT
from raw_client import MCPError, RawHTTPMCPClient

MEMORY_SERVER = str(ROOT / "server" / "memory_server.py")
TOKEN = "test-token-0123456789"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _serve_http(db, **auth_env):
    port = _free_port()
    env = {
        **os.environ,
        "MCP_TRANSPORT": "http",
        "MCP_AUTH_TOKEN": "",
        "MCP_AUTH_CLIENTS_FILE": "",
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_PORT": str(port),
        "MCP_EMBEDDINGS": "off",
        **auth_env,
    }
    pid_file = db.with_suffix(".pid")
    program = ("import os,sys,runpy; from pathlib import Path; "
               "Path(sys.argv[1]).write_text(str(os.getpid())); sys.argv=sys.argv[2:]; "
               "sys.path.insert(0, str(Path(sys.argv[0]).parent)); runpy.run_path(sys.argv[0], run_name='__main__')")
    proc = subprocess.Popen(
        [sys.executable, "-c", program, str(pid_file), MEMORY_SERVER, str(db)],
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
    try:
        yield url
    finally:
        if pid_file.exists() and proc.poll() is None:
            os.kill(int(pid_file.read_text()), signal.SIGTERM)
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    with _serve_http(tmp_path_factory.mktemp("http") / "memory.db", MCP_AUTH_TOKEN=TOKEN) as url:
        yield url


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
    env = {**os.environ, "MCP_TRANSPORT": "http", "MCP_AUTH_TOKEN": "", "MCP_AUTH_CLIENTS_FILE": "",
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


def test_per_client_tokens_bind_sessions_and_audit_identity(tmp_path):
    db = tmp_path / "clients.db"
    config = tmp_path / "clients.json"
    alice_token, bob_token = "alice-" + "a" * 32, "bob-" + "b" * 32
    config.write_text(json.dumps({"alice": alice_token, "bob": bob_token}), encoding="utf-8")
    with _serve_http(db, MCP_AUTH_CLIENTS_FILE=str(config)) as url:
        alice = RawHTTPMCPClient(url, alice_token)
        bob = RawHTTPMCPClient(url, bob_token)
        alice.initialize()
        bob.initialize()
        impostor = RawHTTPMCPClient(url, bob_token)
        impostor.session_id = alice.session_id
        with pytest.raises(ConnectionError, match="404"):
            impostor.list_tools()
        assert not alice.call_tool("memory_set", {"key": "who", "value": "A", "client_id": "bob"}).get("isError")
        read = bob.call_tool("memory_get", {"key": "who"})
        assert "source: alice" in read["content"][0]["text"]
        failure = bob.call_tool("memory_set", {"key": "who", "value": "B", "require_absent": True})
        assert failure["isError"]
        assert not alice.call_tool("memory_delete", {"key": "who", "client_id": "bob"}).get("isError")
        history = bob.call_tool("memory_history", {"key": "who", "include_reads": True})["content"][0]["text"]
        assert [line.split()[1:3] for line in history.splitlines()] == [
            ["alice", "set:"], ["bob", "get:"], ["bob", "cas_fail:"], ["alice", "delete:"],
        ]
        alice.close()
        bob.close()

    config.write_text(json.dumps({"bob": bob_token}), encoding="utf-8")
    with _serve_http(db, MCP_AUTH_CLIENTS_FILE=str(config)) as url:
        with pytest.raises(MCPError, match="401"):
            RawHTTPMCPClient(url, alice_token).initialize()
        bob = RawHTTPMCPClient(url, bob_token)
        bob.initialize()
        bob.close()


def test_hashed_tokens_reload_scopes_and_revoke_existing_sessions(tmp_path):
    config = tmp_path / "hashed.json"
    db = tmp_path / "hashed.db"
    alice_token, bob_token = "a" * 43, "b" * 43

    def save(alice_scopes, include_bob=True):
        clients = {"alice": {"token_sha256": hashlib.sha256(alice_token.encode()).hexdigest(),
                             "scopes": alice_scopes}}
        if include_bob:
            clients["bob"] = {"token_sha256": hashlib.sha256(bob_token.encode()).hexdigest(),
                              "scopes": ["read", "write"]}
        staged = config.with_suffix(".new")
        staged.write_text(json.dumps(clients), encoding="utf-8")
        staged.replace(config)

    save(["read"])
    with _serve_http(db, MCP_AUTH_CLIENTS_FILE=str(config)) as url:
        alice, bob = RawHTTPMCPClient(url, alice_token), RawHTTPMCPClient(url, bob_token)
        alice.initialize()
        bob.initialize()
        assert not bob.call_tool("memory_set", {"key": "scoped", "value": "private-value"}).get("isError")
        assert "private-value" in alice.call_tool("memory_get", {"key": "scoped"})["content"][0]["text"]
        for tool, arguments in [
            ("memory_set", {"key": "scoped", "value": "forbidden"}),
            ("memory_delete", {"key": "scoped"}),
        ]:
            result = alice.call_tool(tool, arguments)
            assert result["isError"]
            assert "write scope" in result["content"][0]["text"]
        save(["read", "write"])
        assert not alice.call_tool("memory_set", {"key": "scoped", "value": "allowed"}).get("isError")
        save(["read"])
        assert alice.call_tool("memory_delete", {"key": "scoped"})["isError"]
        save(["read"], include_bob=False)
        with pytest.raises(MCPError, match="401"):
            bob.list_tools()

        def get(path, token=alice_token, **extra):
            req = urllib.request.Request(url.removesuffix("/mcp") + path,
                                         headers={"Authorization": f"Bearer {token}", **extra})
            return urllib.request.urlopen(req, timeout=10)

        with get("/healthz") as response:
            assert json.load(response) == {"status": "ok"}
        with get("/metrics") as response:
            metrics = response.read().decode()
            assert "memory_auth_failures_total 1" in metrics
            assert "memory_lock_wait_seconds_count" in metrics
            assert "memory_chain_length" in metrics
            assert "private-value" not in metrics
        with pytest.raises(urllib.error.HTTPError) as error:
            get("/healthz", Host="evil.example")
        assert error.value.code == 421
        config.write_text("{invalid json", encoding="utf-8")
        with pytest.raises(ConnectionError, match="503"):
            alice.list_tools()
        save(["read"])
        assert alice.list_tools()
        alice.close()


def test_debug_log_omits_values_and_failure_details(tmp_path):
    db, debug = tmp_path / "debug.db", tmp_path / "debug.jsonl"
    with _serve_http(db, MCP_AUTH_TOKEN=TOKEN, MCP_DEBUG_LOG=str(debug)) as url:
        client = RawHTTPMCPClient(url, TOKEN)
        client.initialize()
        secret = "sensitive-fact-348792"
        client.call_tool("memory_set", {"key": "secret-key", "value": secret})
        client.call_tool("memory_get", {"key": "secret-key"})
        client.call_tool("memory_set", {"key": "secret-key", "value": "new", "expected_value": "wrong"})
        client.close()
    contents = debug.read_text(encoding="utf-8")
    assert secret not in contents and "secret-key" not in contents and TOKEN not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    assert len(events) == 3
    assert events[-1]["outcome"] == "ToolError"
