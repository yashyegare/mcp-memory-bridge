"""Measure embedded HTTP writes alongside unlogged reads in one server process."""

import argparse
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
from raw_client import RawHTTPMCPClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=ROOT / "server" / "memory_server.py")
    parser.add_argument("--clients", type=int, default=4, help="even number, half readers and half writers")
    parser.add_argument("--operations", type=int, default=100)
    args = parser.parse_args()
    if args.clients < 2 or args.clients % 2 or args.operations < 1:
        parser.error("use an even --clients >= 2 and --operations >= 1")
    socket.setdefaulttimeout(120)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with TemporaryDirectory() as temp:
        pid_file, db = Path(temp) / "pid", Path(temp) / "memory.db"
        program = (
            "import os,sys,runpy; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); sys.argv=sys.argv[2:]; "
            "sys.path.insert(0, str(Path(sys.argv[0]).parent)); runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        token = "benchmark-local-token-" + "a" * 32
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(pid_file), str(args.server.resolve()), str(db)],
            env={**os.environ, "MCP_TRANSPORT": "http", "MCP_EMBEDDINGS": "1", "MCP_LOG_READS": "off",
                 "MCP_AUTH_TOKEN": token, "MCP_AUTH_CLIENTS_FILE": "", "MCP_HTTP_HOST": "127.0.0.1",
                 "MCP_HTTP_PORT": str(port), "MCP_SQLITE_SYNCHRONOUS": "NORMAL", "MCP_DEBUG_LOG": ""},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        clients = []
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("HTTP benchmark server did not start")
                    time.sleep(0.1)
            for _ in range(args.clients):
                client = RawHTTPMCPClient(f"http://127.0.0.1:{port}/mcp", token)
                client.initialize()
                clients.append(client)
            warmup = clients[0].call_tool("memory_set", {"key": "probe", "value": "warm up the embedding model"})
            if warmup.get("isError"):
                raise RuntimeError(f"model warmup failed: {warmup}")
            barrier = Barrier(args.clients)

            def worker(index):
                role = "read" if index % 2 else "write"
                samples = []
                barrier.wait()
                start = time.perf_counter()
                for number in range(args.operations):
                    before = time.perf_counter()
                    result = clients[index].call_tool(
                        "memory_get" if role == "read" else "memory_set",
                        {"key": "probe"} if role == "read" else {"key": "probe", "value": f"fact {index}/{number}"},
                    )
                    if result.get("isError"):
                        raise RuntimeError(f"benchmark tool failed: {result}")
                    samples.append((time.perf_counter() - before) * 1000)
                return role, samples, start, time.perf_counter()

            with ThreadPoolExecutor(max_workers=args.clients) as pool:
                results = list(pool.map(worker, range(args.clients)))
            elapsed = max(r[3] for r in results) - min(r[2] for r in results)
            output = {"clients": args.clients, "operations_per_client": args.operations,
                      "operations_per_second": round(args.clients * args.operations / elapsed, 2)}
            for role in ("read", "write"):
                samples = sorted(x for label, batch, *_ in results if label == role for x in batch)
                output[role + "_p50_ms"] = round(samples[math.ceil(len(samples) * 0.5) - 1], 3)
                output[role + "_p99_ms"] = round(samples[math.ceil(len(samples) * 0.99) - 1], 3)
            print(json.dumps(output))
        finally:
            for client in clients:
                client.close()
            if pid_file.exists() and process.poll() is None:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
