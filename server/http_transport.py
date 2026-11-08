"""Reloadable HTTP credentials, transport protection, and operational endpoints."""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

LOGGER = logging.getLogger("memory.auth")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential configuration key")
        result[key] = value
    return result


class Credentials:
    """Read a complete configuration on every request so revocations apply immediately."""
    def __init__(self, path: str, shared_token: str):
        self.path = path
        self.shared_digest = hashlib.sha256(shared_token.encode("utf-8")).hexdigest()
        if bool(path) == bool(shared_token):
            raise ValueError("HTTP requires exactly one of MCP_AUTH_TOKEN or MCP_AUTH_CLIENTS_FILE")
        if shared_token and not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", shared_token):
            raise ValueError("invalid shared bearer-token characters")
        self.load()  # Fail closed at startup too.

    def load(self) -> list[tuple[str, str | None, list[str]]]:
        if not self.path:
            return [(self.shared_digest, None, ["read", "write"])]
        clients = json.loads(Path(self.path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(clients, dict):
            raise ValueError("expected an object mapping client IDs to credentials")
        credentials = []
        seen = set()
        for client_id, entry in clients.items():
            if not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", client_id):
                raise ValueError("invalid client ID")
            if isinstance(entry, str):
                # Compatibility with the original per-client configuration.
                if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,}", entry):
                    raise ValueError("legacy tokens need at least 32 bearer-token characters")
                digest, scopes = hashlib.sha256(entry.encode("ascii")).hexdigest(), ["read", "write"]
            elif isinstance(entry, dict) and set(entry) == {"token_sha256", "scopes"}:
                digest, scopes = entry["token_sha256"], entry["scopes"]
            else:
                raise ValueError("expected token_sha256 and scopes")
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("token_sha256 must be a lowercase SHA-256 digest")
            if not isinstance(scopes, list) or scopes not in (["read"], ["read", "write"], ["write", "read"]):
                raise ValueError("scopes must grant read, or read and write")
            if digest in seen:
                raise ValueError("each client must have a unique token hash")
            seen.add(digest)
            credentials.append((digest, client_id, scopes))
        return credentials

    def authenticate(self, headers: list[bytes]):
        credentials = self.load()
        if len(headers) != 1 or not headers[0].startswith(b"Bearer "):
            return None
        digest = hashlib.sha256(headers[0][7:]).hexdigest()
        matches = [(client_id, scopes) for expected, client_id, scopes in credentials
                   if hmac.compare_digest(digest, expected)]
        return matches[0] if matches else None


def authenticated_app(app, credentials: Credentials, security, metrics):
    protection = TransportSecurityMiddleware(security)

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        start = time.perf_counter()
        try:
            headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            try:
                if credentials.path:
                    principal = await anyio.to_thread.run_sync(credentials.authenticate, headers)
                else:
                    principal = credentials.authenticate(headers)
            except (OSError, ValueError):
                metrics.add("auth_config_failures_total")
                LOGGER.warning("HTTP authentication configuration unavailable")
                await JSONResponse({"error": "authentication configuration unavailable"}, status_code=503)(
                    scope, receive, send)
                return
            if principal is None:
                metrics.add("auth_failures_total")
                LOGGER.warning("HTTP authentication rejected")
                await JSONResponse({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32001, "message": "Unauthorized: missing or invalid bearer token"},
                }, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
            client_id, scopes = principal
            if client_id is not None:
                user = AuthenticatedUser(AccessToken(token="", client_id=client_id, scopes=scopes))
                scope = {**scope, "user": user}
            # Custom operational routes receive the same Host/Origin protection.
            rejected = await protection.validate_request(Request(scope))
            if rejected:
                await rejected(scope, receive, send)
                return
            await app(scope, receive, send)
        finally:
            metrics.observe("http_request", time.perf_counter() - start)
    return middleware


def run(mcp, state, metrics) -> None:
    try:
        credentials = Credentials(os.environ.get("MCP_AUTH_CLIENTS_FILE", ""), os.environ.get("MCP_AUTH_TOKEN", ""))
    except (OSError, ValueError):
        print("refusing to start: configure exactly one valid MCP_AUTH_TOKEN or MCP_AUTH_CLIENTS_FILE",
              file=sys.stderr)
        sys.exit(2)
    import uvicorn

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request):
        try:
            await anyio.to_thread.run_sync(state)
            return JSONResponse({"status": "ok"})
        except (sqlite3.Error, ValueError):
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @mcp.custom_route("/metrics", methods=["GET"])
    async def exposition(request):
        try:
            values = await anyio.to_thread.run_sync(state)
        except (sqlite3.Error, ValueError):
            return PlainTextResponse("database unavailable\n", status_code=503)
        return PlainTextResponse(metrics.render(values), media_type="text/plain; version=0.0.4")

    public_hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *public_hosts],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         *[f"https://{h}" for h in public_hosts]],
    )
    app = mcp.streamable_http_app(json_response=True, transport_security=security)
    uvicorn.run(authenticated_app(app, credentials, security, metrics),
                host=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_HTTP_PORT", "8000")), log_level="warning")
