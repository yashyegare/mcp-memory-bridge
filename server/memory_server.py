"""Compatibility launcher for existing configs that run this path directly.

The server now lives in the `mcp_memory_bridge` package. Prefer
`mcp-memory-bridge [db]` or `python -m mcp_memory_bridge [db]`; this file
keeps `python server/memory_server.py [db]` working, installed or not.
"""

import sys
from pathlib import Path

try:
    from mcp_memory_bridge.server import main
except ModuleNotFoundError as exc:
    if exc.name != "mcp_memory_bridge":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from mcp_memory_bridge.server import main

if __name__ == "__main__":
    main()
