"""
The "make it actually break" demo (Phase 3).

Two clients share one SQLite file. Client A calls lock_hold(3) -- the demo
tool that grabs the write lock and holds it -- while client B tries to
memory_set. What B experiences is determined entirely by busy_timeout:

  MCP_SQLITE_BUSY_TIMEOUT=0    -> B's write fails fast with a tool-level
                                  error: "database is locked ..."
  MCP_SQLITE_BUSY_TIMEOUT=5000 -> B's write blocks ~3s, then succeeds
                                  (the lock was released before the
                                  timeout expired) -- queuing, not error.

Run both and compare:
    venv\\Scripts\\python.exe tools\\lock_demo.py            # fail-fast mode
    venv\\Scripts\\python.exe tools\\lock_demo.py --queue    # queuing mode
"""

import argparse
import sys
import time

sys.path.insert(0, "client")
from raw_client import RawMCPClient


def _text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="lock_demo.db")
    parser.add_argument("--queue", action="store_true", help="busy_timeout=5000 (default) instead of 0")
    parser.add_argument("--hold", type=float, default=3.0, help="seconds client A holds the lock")
    args = parser.parse_args()

    timeout_ms = 5000 if args.queue else 0
    mode = "QUEUING (busy_timeout=5000ms)" if args.queue else "FAIL-FAST (busy_timeout=0ms)"
    common_env = {"MCP_ENABLE_DEMO_TOOLS": "1", "MCP_SQLITE_BUSY_TIMEOUT": str(timeout_ms)}

    print(f"mode: {mode}")

    client_a = RawMCPClient(
        [sys.executable, "server/memory_server.py", args.db],
        env={**common_env, "MCP_CLIENT_ID": "client-A"},
    )
    client_b = RawMCPClient(
        [sys.executable, "server/memory_server.py", args.db],
        env={**common_env, "MCP_CLIENT_ID": "client-B"},
    )
    try:
        client_a.initialize()
        client_b.initialize()

        # Warm the schema on one connection first (first-write creates the
        # tables; two concurrent first-writes would just race harmlessly).
        client_a.call_tool("memory_set", {"key": "warmup", "value": "ok"})

        # Client A grabs the write lock and holds it. The call returns when
        # the lock is RELEASED, so client B must be launched mid-hold: run A
        # on a thread, give it a beat to grab the lock, then fire B.
        import threading

        a_result: dict = {}

        def run_a():
            a_result["r"] = client_a.call_tool("lock_hold", {"seconds": args.hold})

        print(f"client A: lock_hold({args.hold}s) ...")
        thread = threading.Thread(target=run_a)
        thread.start()
        time.sleep(0.7)  # A is now inside its write transaction, holding the lock

        print("client B: memory_set while A holds the lock ...")
        start = time.monotonic()
        # call_tool returns the raw result; tool-level failures arrive as a
        # SUCCESS response with isError=true, so classify on that, not on
        # exceptions (those are protocol-level only).
        result = client_b.call_tool("memory_set", {"key": "contended", "value": "B was here"})
        elapsed = time.monotonic() - start
        if result.get("isError"):
            print(f"  B FAILED after {elapsed:.2f}s (tool-level error):")
            print(f"    {_text(result)}")
            if not args.queue:
                print("  => the write was REJECTED immediately: busy_timeout=0 means no waiting.")
            else:
                print("  => still failed after the 5s timeout: the lock was held longer than that.")
        else:
            print(f"  B SUCCEEDED after {elapsed:.2f}s: {_text(result)!r}")
            print("  => the write WAITED for the lock, then committed. That's queuing.")

        thread.join()
        print(f"client A finished: {_text(a_result['r'])!r}")

        print()
        print("audit trail for the contended key (attributed, ordered):")
        trail = client_a.call_tool("memory_get", {"key": "contended", "include_events": True})
        if trail.get("isError"):
            print(f"  ({_text(trail)})")
            print("  (B's rejected write never landed -- nothing to show, which is itself the lesson)")
        else:
            print(_text(trail))
    finally:
        client_a.close()
        client_b.close()


if __name__ == "__main__":
    main()
