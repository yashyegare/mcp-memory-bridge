"""Compatibility launcher: `python tools/memory_admin.py ...` still works.

Prefer the installed `mcp-memory-admin` command.
"""

import sys
from pathlib import Path

try:
    from mcp_memory_bridge.admin import main
except ModuleNotFoundError as exc:
    if exc.name != "mcp_memory_bridge":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from mcp_memory_bridge.admin import main

if __name__ == "__main__":
    sys.exit(main())
