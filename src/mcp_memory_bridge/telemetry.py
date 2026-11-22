"""Bounded process counters and debug records that omit memory contents."""

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = defaultdict(float)
        for name in ("auth_failures_total", "auth_config_failures_total", "tool_failures_total", "debug_log_failures_total"):
            self._values[name] = 0

    def add(self, name: str, value: float = 1) -> None:
        with self._lock:
            self._values[name] += value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self._values[name + "_seconds_sum"] += seconds
            self._values[name + "_seconds_count"] += 1

    def render(self, state: dict) -> str:
        with self._lock:
            values = dict(self._values)
        values.update(state)
        return "".join(f"memory_{name} {value}\n" for name, value in sorted(values.items()))


METRICS = Metrics()


def debug_event(tool: str, start: float, error: BaseException | None = None) -> None:
    path = os.environ.get("MCP_DEBUG_LOG")
    if not path:
        return
    # Arguments, results, exception messages, and tracebacks can all contain
    # fact values or credentials. Only emit fixed metadata.
    event = {
        "time": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
        "outcome": type(error).__name__ if error else "ok",
    }
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")
    except OSError:
        METRICS.add("debug_log_failures_total")
