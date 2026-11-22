"""Shared SQLite-backed memory for MCP clients."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-memory-bridge")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+source"
