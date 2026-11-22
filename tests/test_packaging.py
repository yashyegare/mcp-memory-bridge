"""The package entry points, the compatibility launchers, and lean-install
behavior when the [semantic] extra is missing."""

import subprocess
import sys

from conftest import ROOT, SERVER_CMD


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def test_version_flag():
    result = subprocess.run([*SERVER_CMD, "--version"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("mcp-memory-bridge ")


def test_legacy_server_path_still_launches(make_client, tmp_path):
    # Existing Claude Desktop configs point at server/memory_server.py.
    client = make_client(
        [sys.executable, str(ROOT / "server" / "memory_server.py"), str(tmp_path / "m.db")],
        env={"MCP_EMBEDDINGS": "off"},
    )
    client.initialize()
    client.call_tool("memory_set", {"key": "legacy", "value": "ok"})
    assert "legacy = ok" in _text(client.call_tool("memory_get", {"key": "legacy"}))


def test_legacy_admin_path_still_runs(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "memory_admin.py"), "keygen", str(tmp_path / "k.key")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "k.key").stat().st_size > 0


def test_embeddings_forced_on_without_extra_explains_the_fix(make_client, tmp_path):
    # Hide sentence_transformers even if it is installed: a module set to
    # None in sys.modules makes `import sentence_transformers` raise.
    hide = (
        "import sys; sys.modules['sentence_transformers'] = None; "
        "from mcp_memory_bridge.server import main; main(sys.argv[1:])"
    )
    client = make_client(
        [sys.executable, "-c", hide, str(tmp_path / "m.db")],
        env={"MCP_EMBEDDINGS": "1"},
    )
    client.initialize()
    result = client.call_tool("memory_set", {"key": "k", "value": "v"})
    assert result.get("isError") is True
    assert "mcp-memory-bridge[semantic]" in _text(result)
