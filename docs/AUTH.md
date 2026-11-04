# Auth & trust model for the HTTP transport

The stdio server never needed auth: the OS *is* the authentication. A host
spawns the server as a child process, so only something with filesystem
access could talk to it — and anything with filesystem access could open the
SQLite file directly. HTTP changes that completely: the moment the server
listens on a socket, "who is allowed to call this" becomes a real question.

## Configure per-client credentials

Set `MCP_TRANSPORT=http` and `MCP_AUTH_CLIENTS_FILE` to the absolute path
of a private JSON file. Unset `MCP_AUTH_TOKEN` in this mode. The server
requires exactly one credential source at startup.

```json
{
  "claude-desktop": "<unique-generated-token>",
  "ci-agent": "<another-unique-generated-token>"
}
```

Generate each token independently with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Client IDs use 1 to 64 ASCII letters, digits, dots, underscores, or
hyphens. Keep the file outside the repository and restrict access to the
server account. For the systemd deployment, place it in `/opt/mcp-memory`
and update `/opt/mcp-memory/env` before restarting the service.

Give each client only its own token. Every request carries it in the
`Authorization` header. The server checks the credential before passing
the request to MCP and binds the session to the configured identity.
Facts and audit records use that identity even when tool arguments
include a different `client_id`. A valid credential grants access to
the shared store. Attribution identifies the credential holder, and
anyone who obtains that credential can act as its client.

To revoke a client, remove its entry and restart the server. To rotate
its token, replace the value and restart. The file loads at startup and
the restart ends active sessions, so remaining clients must reconnect.
Serve remote traffic through HTTPS and keep the HTTP listener on
loopback behind the TLS proxy.

## Shared-token compatibility

Existing HTTP deployments can continue using `MCP_AUTH_TOKEN` alone.
In that mode, clients share one credential and attribution comes from
the tool arguments or `MCP_CLIENT_ID`. Stdio also uses self-reported
attribution.

## Header walkthrough (what each one is for)

| Header | Direction | Purpose |
|---|---|---|
| `Authorization: Bearer <t>` | client → server | the credential itself |
| `Accept: application/json` | client → server | requests single JSON responses (no SSE stream) |
| `Content-Type: application/json` | client → server | framing on the way in |
| `Mcp-Session-Id` | server → client, then echoed back | names the server-side session created at `initialize` |
| `MCP-Protocol-Version` | client → server on later calls | tells the server which negotiated version is in use |
| `WWW-Authenticate: Bearer` | server → client (401) | machine-readable "you need a token" |
