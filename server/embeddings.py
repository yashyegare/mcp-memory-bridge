"""Lazy, serialized local embedding model access."""

import os
import sys
import threading
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

EMBEDDINGS_ENABLED = os.environ.get("MCP_EMBEDDINGS", "1") != "off"

_model = None
_model_lock = threading.Lock()  # lazy load must be single-flight (preload thread vs first call)
_encode_lock = threading.Lock()


def _hf_model_cached(model_id: str) -> bool:
    """Is model_id already in the local HF cache? Pure filesystem check.

    Deliberately does NOT import huggingface_hub: that library (and
    transformers) snapshot HF_HUB_OFFLINE from the environment at import
    time, so setting the variable after any HF-family import silently does
    nothing -- which is exactly the trap this check originally fell into
    (the env was set, but only after the import, so the model load went
    online anyway and hung on network freshness checks).
    """
    home = Path.home() / ".cache" / "huggingface"
    hub = Path(os.environ.get("HF_HUB_CACHE",
               Path(os.environ.get("HF_HOME", home)) / "hub"))
    snapshots = hub / ("models--" + model_id.replace("/", "--")) / "snapshots"
    try:
        return snapshots.is_dir() and any(snapshots.iterdir())
    except OSError:
        return False


def _embed(texts: list[str]):
    """Embed texts as normalized float32 numpy vectors (cosine-ready)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # double-checked: only one thread ever loads
                if not EMBEDDINGS_ENABLED:
                    raise ToolError(
                        "semantic search is disabled on this server (MCP_EMBEDDINGS=off); "
                        "use memory_list with a key prefix instead"
                    )
                # Offline-vs-online decided BEFORE any HF import (see
                # _hf_model_cached): cached -> force offline so network
                # freshness checks can never hang a tool call (observed);
                # not cached -> stay online so the one-time download works
                # (fresh clones, CI).
                if _hf_model_cached("sentence-transformers/all-MiniLM-L6-v2"):
                    os.environ.setdefault("HF_HUB_OFFLINE", "1")
                    print("model cache detected: HF_HUB_OFFLINE=1", file=sys.stderr)
                from sentence_transformers import SentenceTransformer  # deferred heavy import

                _model = SentenceTransformer("all-MiniLM-L6-v2")
    # show_progress_bar=False: ST's default tqdm bar writes to stderr on
    # every encode. Harmless in a terminal, but a real host captures stderr
    # through a pipe -- enough per-call refreshes fill the OS pipe buffer
    # (~64KB) and BLOCK the server mid-encode (the Phase 3 wedge, caused
    # this time by the embedding path itself).
    with _encode_lock:
        vectors = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return vectors.astype("float32")


def _maybe_preload_model() -> None:
    """With MCP_PRELOAD_MODEL=1, load the embedding model in a background
    thread at startup: the server still starts instantly, but the model is
    (usually) ready by the first real tool call, so clients never see the
    ~25s cold load on their first memory_set."""
    if not (EMBEDDINGS_ENABLED and os.environ.get("MCP_PRELOAD_MODEL") == "1"):
        return

    def _load():
        try:
            _embed(["preload"])
            print("embedding model preloaded", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - preload falls back to lazy loading
            print(f"embedding model preload failed: {exc}", file=sys.stderr)

    threading.Thread(target=_load, daemon=True).start()
