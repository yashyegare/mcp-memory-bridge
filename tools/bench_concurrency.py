"""Measure warm write latency for concurrent stdio MCP clients sharing SQLite."""

import argparse
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
from raw_client import MCPError, RawMCPClient  # noqa: E402


def _positive(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=_positive, default=8)
    parser.add_argument("--writes", type=_positive, default=100, help="timed writes per client")
    args = parser.parse_args()
    barrier = Barrier(args.clients)

    def writer(client: RawMCPClient):
        samples = []
        failures = 0
        first_error = None
        barrier.wait()
        start = time.perf_counter()
        for index in range(args.writes):
            before = time.perf_counter()
            try:
                result = client.call_tool("memory_set", {"key": "bench/shared", "value": str(index)})
                if result.get("isError"):
                    failures += 1
                    first_error = first_error or str(result["content"])
                else:
                    samples.append((time.perf_counter() - before) * 1000)
            except (MCPError, ConnectionError, TimeoutError) as exc:
                failures += 1
                first_error = first_error or str(exc)
        return samples, failures, start, time.perf_counter(), first_error

    with TemporaryDirectory() as temp, ExitStack() as stack:
        clients = []
        for index in range(args.clients):
            client = RawMCPClient(
                [sys.executable, str(ROOT / "server" / "memory_server.py"), str(Path(temp) / "bench.db")],
                env={"MCP_TRANSPORT": "stdio", "MCP_EMBEDDINGS": "off",
                     "MCP_SQLITE_BUSY_TIMEOUT": "5000", "MCP_CLIENT_ID": f"bench-{index}"},
            )
            stack.callback(client.close)
            client.initialize()
            warmup = client.call_tool("memory_set", {"key": "bench/shared", "value": "warmup"})
            if warmup.get("isError"):
                parser.exit(1, f"warmup failed: {warmup['content']}\n")
            clients.append(client)
        with ThreadPoolExecutor(max_workers=args.clients) as pool:
            results = list(pool.map(writer, clients))

    samples = sorted(sample for batch, *_ in results for sample in batch)
    failures = sum(result[1] for result in results)
    elapsed = max(result[3] for result in results) - min(result[2] for result in results)
    print(f"{args.clients} clients x {args.writes} writes | {len(samples)} succeeded | "
          f"{failures} failed | {len(samples) / elapsed:.1f} successful writes/s")
    if samples:
        p50 = samples[math.ceil(0.50 * len(samples)) - 1]
        p99 = samples[math.ceil(0.99 * len(samples)) - 1]
        print(f"successful write latency | p50={p50:.2f} ms | p99={p99:.2f} ms")
    if failures:
        print(f"first failure: {next(result[4] for result in results if result[1])}", file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
