"""
Live two-client hammer for the Claude Desktop experiment.

Writes `live/color` continuously for a fixed window, attributing every
write to "raw-hammer", while YOU ask Claude Desktop (second client, same
SQLite file) to write the same key mid-window. Afterwards, the shared
events log shows the interleaving: hammer writes, Desktop's write(s),
and who last-write-wins.

Built to survive a wedged run: every call's outcome is appended to a log
file the moment it happens, so even if the client dies mid-run the
timeline survives on disk. Stderr is drained (devnull) — see the
"undrained stderr wedge" section of the README for why.

Usage:
    venv\\Scripts\\python.exe tools\\live_hammer.py [--seconds 60] [--db memory.db]
"""

import argparse
import json
import sys
import time

sys.path.insert(0, "client")
from raw_client import MCPError, RawMCPClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--key", default="live/color")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between writes")
    args = parser.parse_args()

    deadline = time.monotonic() + args.seconds
    log_path = "hammer_timeline.log"

    client = RawMCPClient(
        [sys.executable, "server/memory_server.py", args.db],
        default_timeout=5.0,
        capture_stderr=False,  # drain: an undrained stderr wedges the server under sustained errors
        env={"MCP_CLIENT_ID": "raw-hammer"},
    )
    stats = {"sets": 0, "get_saw_desktop": 0, "get_saw_own": 0, "errors": 0}
    with open(log_path, "w", encoding="utf-8") as log:
        def record(event: str, **fields) -> None:
            entry = {"t": round(time.time(), 3), "event": event, **fields}
            log.write(json.dumps(entry) + "\n")
            log.flush()
            print(json.dumps(entry), flush=True)

        try:
            client.initialize()
            record("start", key=args.key, seconds=args.seconds)
            n = 0
            while time.monotonic() < deadline:
                value = f"hammer-{n}"
                try:
                    result = client.call_tool("memory_set", {"key": args.key, "value": value})
                    if result.get("isError"):
                        stats["errors"] += 1
                        record("set_tool_error", text=str(result.get("content")))
                    else:
                        stats["sets"] += 1
                        record("set", value=value)
                        got = client.call_tool("memory_get", {"key": args.key})
                        text = str(got.get("content", ""))
                        if "claude-desktop" in text:
                            stats["get_saw_desktop"] += 1
                            record("observed_desktop_value", text=text[:200])
                        elif value in text:
                            stats["get_saw_own"] += 1
                except (MCPError, ConnectionError) as exc:
                    stats["errors"] += 1
                    record("error", type=type(exc).__name__, message=str(exc)[:200])
                n += 1
                time.sleep(args.interval)
            record("done", stats=stats)
            print(f"\nsummary: {stats}")
            print(f"timeline in {log_path}; shared db: {args.db}")
        finally:
            client.close()


if __name__ == "__main__":
    main()
