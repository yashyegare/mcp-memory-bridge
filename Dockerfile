# Lean memory server image: no ML stack (MCP_EMBEDDINGS=off baked in).
# The semantic-search variant needs ~2GB of torch — overkill for a stdio
# key/value server; use it via the venv install instead.
#
# A stdio MCP server in Docker is useful when the *host* launches it as a
# subprocess, e.g. claude_desktop_config.json:
#   "command": "docker",
#   "args": ["run", "--rm", "-i", "-v", "mcp-memory-data:/data", "mcp-memory-bridge"]
FROM python:3.13-slim

WORKDIR /app

# Lean mode: the package without the [semantic] extra, at the versions CI
# tests (requirements.txt is used as a constraints file).
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir -c requirements.txt . \
    && rm -rf src

RUN groupadd --gid 10001 memory \
    && useradd --uid 10001 --gid memory --no-create-home memory \
    && mkdir -p /data \
    && chown memory:memory /data
USER 10001:10001

ENV MCP_EMBEDDINGS=off
# stdout is the JSON-RPC protocol channel; Python must not buffer it or the
# host sees no responses until exit.
ENV PYTHONUNBUFFERED=1

CMD ["mcp-memory-bridge", "/data/memory.db"]
