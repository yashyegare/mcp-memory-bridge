"""Performance benchmarks: keep the README's claims honest as the store grows.

Thresholds refer to WARM latency (model loaded, schema created), measured as
the median of several iterations to absorb scheduler noise. Local-machine
numbers are what the README claims; CI gets a generous multiplier because
shared runners are slow and noisy -- the point is catching a pathological
regression (e.g. someone adding a per-call model load or an O(n^2) scan),
not millisecond precision."""

import importlib.util
import os
import statistics
import sys
import time

import pytest

from conftest import ROOT

MEMORY_SERVER = str(ROOT / "server" / "memory_server.py")
ST_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None
ON_CI = os.environ.get("CI") == "true"

# Local targets from the README claims; CI runners get 4x headroom.
SET_LIMIT_MS = 100 * (4 if ON_CI else 1)
SEARCH_LIMIT_MS = 50 * (4 if ON_CI else 1)
WARMUP = 3
MEASURED = 7


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def _median_ms(fn, *args, **kwargs) -> float:
    for _ in range(WARMUP):
        fn(*args, **kwargs)
    samples = []
    for _ in range(MEASURED):
        start = time.perf_counter()
        fn(*args, **kwargs)
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


@pytest.mark.skipif(not ST_AVAILABLE, reason="sentence-transformers not installed")
def test_warm_write_and_search_latency(make_client, tmp_path):
    """Warm memory_set < 100ms and memory_search < 50ms (median), including
    the embedding write, across a realistically-sized store."""
    client = make_client(
        [sys.executable, MEMORY_SERVER, str(tmp_path / "memory.db")],
        env={"MCP_CLIENT_ID": "bench"},
        default_timeout=180.0,  # first embedded write may pay the model load
    )
    client.initialize()

    # Seed a store at plausible demo scale (~200 embedded facts) -- this also
    # pays the one-time model load outside the measured section.
    for i in range(200):
        client.call_tool(
            "memory_set",
            {"key": f"bench/item-{i:03d}", "value": f"fact number {i} about topic {i % 7}"},
        )

    set_ms = _median_ms(client.call_tool, "memory_set", {"key": "bench/probe", "value": "a fresh fact for timing"})
    search_ms = _median_ms(client.call_tool, "memory_search", {"query": "a fact about topic 3", "top_k": 3})

    print(f"\n  warm memory_set   median: {set_ms:6.1f} ms (limit {SET_LIMIT_MS} ms)")
    print(f"  warm memory_search median: {search_ms:6.1f} ms (limit {SEARCH_LIMIT_MS} ms)")

    assert set_ms < SET_LIMIT_MS, f"warm memory_set took {set_ms:.1f}ms (limit {SET_LIMIT_MS}ms)"
    assert search_ms < SEARCH_LIMIT_MS, f"warm memory_search took {search_ms:.1f}ms (limit {SEARCH_LIMIT_MS}ms)"
