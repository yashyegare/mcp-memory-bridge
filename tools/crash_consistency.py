"""Force-terminate real MCP servers during writes and verify recovery after each kill."""

import argparse
import os
import random
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "server"))
import recovery  # noqa: E402
from raw_client import RawMCPClient  # noqa: E402


def run(rounds: int, mode: str, seed: int) -> None:
    randomizer = random.Random(seed)
    with TemporaryDirectory() as temp:
        database, pid_file = Path(temp) / "memory.db", Path(temp) / "pid"
        for iteration in range(rounds):
            # Capture the interpreter's PID because Windows venv launchers can
            # introduce an extra process between Popen and the actual server.
            program = (
                "import os,sys,runpy; from pathlib import Path; "
                "Path(sys.argv[1]).write_text(str(os.getpid())); "
                "sys.argv=sys.argv[2:]; sys.path.insert(0, str(Path(sys.argv[0]).parent)); "
                "runpy.run_path(sys.argv[0], run_name='__main__')"
            )
            client = RawMCPClient(
                [sys.executable, "-c", program, str(pid_file), str(ROOT / "server" / "memory_server.py"), str(database)],
                env={"MCP_TRANSPORT": "stdio", "MCP_EMBEDDINGS": "off", "MCP_LOG_READS": "off",
                     "MCP_SQLITE_SYNCHRONOUS": mode},
                default_timeout=10,
            )
            ready = threading.Event()
            killing = threading.Event()
            acknowledged = []
            failures = []

            def write_loop():
                try:
                    for index in range(10000):
                        key = f"round-{iteration}/write-{index}"
                        result = client.call_tool("memory_set", {"key": key, "value": str(index)})
                        if result.get("isError"):
                            failures.append(result)
                            return
                        acknowledged.append((key, str(index)))
                        if index >= 2:
                            ready.set()
                except (ConnectionError, OSError) as exc:
                    if not killing.is_set():
                        failures.append(str(exc))
                except ValueError as exc:
                    failures.append(str(exc))

            writer = None
            try:
                client.initialize()
                writer = threading.Thread(target=write_loop)
                writer.start()
                if not ready.wait(15):
                    raise RuntimeError(f"writer did not make progress: {failures}")
                time.sleep(randomizer.uniform(0.001, 0.025))
                pid = int(pid_file.read_text())
                killing.set()
                os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
                writer.join(timeout=12)
                if writer.is_alive() or failures:
                    raise RuntimeError(f"writer failed: {failures}")
            finally:
                client.close()
                if writer:
                    writer.join(timeout=12)
            with closing(sqlite3.connect(database)) as db:
                db.execute("BEGIN")
                recovery.check_database(db)
                for key, value in acknowledged:
                    if db.execute("SELECT value FROM facts WHERE key=?", (key,)).fetchone() != (value,):
                        raise RuntimeError("an acknowledged write was lost after process termination")
            print(f"{mode} round {iteration + 1}/{rounds}: integrity, chain, replay and {len(acknowledged)} acknowledged writes OK",
                  flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10, help="kill/recovery cycles per mode")
    parser.add_argument("--synchronous", choices=["NORMAL", "FULL", "both"], default="both")
    parser.add_argument("--seed", type=int, default=20261108)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    for mode in (["NORMAL", "FULL"] if args.synchronous == "both" else [args.synchronous]):
        run(args.rounds, mode, args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
