# Lean memory server image: no ML stack (MCP_EMBEDDINGS=off baked in).
# The semantic-search variant needs ~2GB of torch — overkill for a stdio
# key/value server; use it via the venv install instead.
#
# A stdio MCP server in Docker is useful when the *host* launches it as a
# subprocess, e.g. claude_desktop_config.json:
#   "command": "docker",
#   "args": ["run", "--rm", "-i", "-v", "C:\\...\\memory.db:/data/memory.db",
#            "mcp-memory-bridge", "python", "server/memory_server.py", "/data/memory.db"]
FROM python:3.13-slim

WORKDIR /app

# Lean mode: only the two runtime deps the server needs without ML.
COPY requirements.txt .
RUN pip install --no-cache-dir mcp==2.2.0 numpy==2.2.6

COPY server/memory_server.py server/memory_server.py

ENV MCP_EMBEDDINGS=off
# stdout is the JSON-RPC protocol channel; Python must not buffer it or the
# host sees no responses until exit.
ENV PYTHONUNBUFFERED=1

CMD ["python", "server/memory_server.py", "/data/memory.db"]
