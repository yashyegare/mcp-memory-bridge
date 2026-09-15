"""Phase 4 tests: semantic recall (A) and the audit-trail history tool (B).

The positive semantic tests are combined into one test on purpose: every
test spawns a fresh server subprocess, so each would pay the embedding
model's load cost separately. One test, one load."""

import importlib.util
import sys

import pytest

from conftest import ROOT

MEMORY_SERVER = str(ROOT / "server" / "memory_server.py")
ST_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def _memory_client(make_client, tmp_path, extra_env=None):
    # Default to embeddings OFF (fast, no model load); a test that explicitly
    # passes MCP_EMBEDDINGS in extra_env opts in — extra_env wins.
    client = make_client(
        [sys.executable, MEMORY_SERVER, str(tmp_path / "memory.db")],
        env={"MCP_EMBEDDINGS": "off", **(extra_env or {})},
    )
    client.initialize()
    return client


# ---------------------------------------------------------------------- #
# Phase 4B: memory_history                                               #
# ---------------------------------------------------------------------- #

def test_history_shows_writes_and_delete_with_attribution(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "h/k", "value": "first", "client_id": "cursor"})
    client.call_tool("memory_set", {"key": "h/k", "value": "second", "client_id": "claude-desktop"})
    client.call_tool("memory_delete", {"key": "h/k", "client_id": "cursor"})

    history = _text(client.call_tool("memory_history", {"key": "h/k"}))
    assert "cursor" in history and "claude-desktop" in history
    assert "first" in history and "second" in history and "delete" in history
    # In order: first write, overwrite, delete.
    assert history.index("first") < history.index("second") < history.index("delete")


def test_history_include_reads_adds_gets(make_client, tmp_path):
    client = _memory_client(make_client, tmp_path)
    client.call_tool("memory_set", {"key": "h/r", "value": "v"})
    client.call_tool("memory_get", {"key": "h/r"})

    without = _text(client.call_tool("memory_history", {"key": "h/r"}))
    assert "get" not in without
    with_reads = _text(client.call_tool("memory_history", {"key": "h/r", "include_reads": True}))
    assert "get" in with_reads


# ---------------------------------------------------------------------- #
# Phase 4A: memory_search                                                #
# ---------------------------------------------------------------------- #

def test_search_disabled_is_a_readable_tool_error(make_client, tmp_path):
    """With MCP_EMBEDDINGS=off the server degrades gracefully: memory_search
    returns a tool-level error that explains what to do instead (mcp 2.x
    keeps ToolError messages; anything else gets flattened)."""
    client = _memory_client(make_client, tmp_path, {"MCP_EMBEDDINGS": "off"})
    result = client.call_tool("memory_search", {"query": "anything"})
    assert result.get("isError") is True
    assert "disabled" in _text(result)


@pytest.mark.skipif(not ST_AVAILABLE, reason="sentence-transformers not installed")
def test_semantic_search_full_lifecycle(make_client, tmp_path):
    """Store facts, find one by MEANING with different words, verify the
    search is audit-logged, then verify delete removes the embedding."""
    # The one test that pays the model-load cost (hence the combined shape).
    client = _memory_client(make_client, tmp_path, {"MCP_EMBEDDINGS": "1"})

    client.call_tool("memory_set", {"key": "user/theme", "value": "the user prefers dark mode interfaces"})
    client.call_tool("memory_set", {"key": "pet/name", "value": "Fluffy is a tabby cat"})

    # The payoff: query uses words that appear NOWHERE in the stored facts.
    result = client.call_tool("memory_search", {"query": "appearance preference for screens", "top_k": 2})
    text = _text(result)
    assert result.get("isError") is not True
    assert "user/theme" in text
    # It should rank the theme fact above the cat fact.
    assert text.index("user/theme") < text.index("pet/name")

    # Search events are audit-logged with query + winner + score; they show
    # up in the key's history once reads/searches are included.
    events = _text(client.call_tool("memory_history", {"key": "user/theme", "include_reads": True}))
    assert "search" in events and "appearance preference" in events

    # Delete cascades to the embedding: the deleted fact stops being findable.
    client.call_tool("memory_delete", {"key": "pet/name"})
    after = _text(client.call_tool("memory_search", {"query": "tabby cat", "top_k": 5}))
    assert "pet/name" not in after
