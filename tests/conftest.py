"""Shared fixtures. These are integration tests: every test drives the real
hand-rolled client against a real server subprocess, so they exercise the
actual wire protocol, not mocks."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))  # so `from raw_client import ...` works

# Run against the package source even without `pip install -e .`: this
# process imports it directly, and server subprocesses inherit PYTHONPATH.
# With an editable install both paths name the same files.
SRC = str(ROOT / "src")
sys.path.insert(0, SRC)
os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC, os.environ.get("PYTHONPATH")]))

# How every test launches the memory server: the package entry point.
SERVER_CMD = [sys.executable, "-m", "mcp_memory_bridge"]

import pytest  # noqa: E402
from raw_client import RawMCPClient  # noqa: E402


@pytest.fixture
def make_client(tmp_path):
    """Factory: spawn a server subprocess via the raw client, auto-close.

    Usage:
        client = make_client([str(ROOT / "server/test_server.py")])
        client.initialize()
    """
    clients: list[RawMCPClient] = []

    def _make(server_command: list[str], **kwargs) -> RawMCPClient:
        client = RawMCPClient(server_command, **kwargs)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()
