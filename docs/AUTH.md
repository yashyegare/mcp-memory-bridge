# Auth & trust model for the HTTP transport

The stdio server never needed auth: the OS *is* the authentication. A host
spawns the server as a child process, so only something with filesystem
access could talk to it — and anything with filesystem access could open the
SQLite file directly. HTTP changes that completely: the moment the server
listens on a socket, "who is allowed to call this" becomes a real question.

## Configure per-client credentials

Set `MCP_TRANSPORT=http` and `MCP_AUTH_CLIENTS_FILE` to a private JSON file.
Select exactly one credential source at startup.

```sh
python tools/memory_admin.py token --scope read-write
python tools/memory_admin.py token --scope read
```

Each invocation prints a random bearer token and its credential object. Give the
token to its client and copy the credential object into the configuration under
that client's ID.

```json
{
  "claude-desktop": {
    "token_sha256": "<generated SHA-256 digest>",
    "scopes": ["read", "write"]
  },
  "reader-agent": {
    "token_sha256": "<another generated SHA-256 digest>",
    "scopes": ["read"]
  }
}
```

Client IDs use 1 to 64 ASCII letters, digits, dots, underscores, or hyphens.
Each digest must be unique. Keep the configuration outside the repository and
restrict writes to the operator. For systemd, place it in `/opt/mcp-memory`
and set its path in `/opt/mcp-memory/env`. The service account needs read access.

The server reads a complete configuration on each HTTP request. Publish changes
by replacing the file atomically. Removing a client revokes its next request,
including requests on an existing MCP session. Token rotation and scope changes
also apply without a restart. A malformed or unreadable configuration produces
503 until repaired. An empty object revokes every per-client credential.

Every request carries `Authorization: Bearer <token>`. The server hashes the
supplied token and compares digests in constant time. It assigns the configured
identity to fact sources and audit records, overriding `client_id` arguments.
The SDK binds sessions to that identity. A client cannot reuse another identity's
session ID. Scope changes are checked using the current request's credential.

The read scope permits get, list, history, and semantic search. Read/write
credentials additionally permit set, delete, and the optional lock demo.
Read operations can still append audit events. All clients share one namespace.

Keep remote traffic behind HTTPS and bind the HTTP listener to loopback behind
the TLS proxy. Failed authentication attempts increment a metric and emit a
generic stderr record. Tokens and memory contents are omitted from those logs.
Authenticated operational endpoints retain Host and Origin checks.

## Shared-token compatibility

Existing HTTP deployments can continue using `MCP_AUTH_TOKEN` alone.
In that mode, clients share one credential and attribution comes from
the tool arguments or `MCP_CLIENT_ID`. Stdio also uses self-reported
attribution. Legacy per-client plaintext token entries remain readable for
existing deployments. New configurations should use the generated hash entries.

## Header walkthrough (what each one is for)

| Header | Direction | Purpose |
|---|---|---|
| `Authorization: Bearer <t>` | client → server | the credential itself |
| `Accept: application/json` | client → server | requests single JSON responses (no SSE stream) |
| `Content-Type: application/json` | client → server | framing on the way in |
| `Mcp-Session-Id` | server → client, then echoed back | names the server-side session created at `initialize` |
| `MCP-Protocol-Version` | client → server on later calls | tells the server which negotiated version is in use |
| `WWW-Authenticate: Bearer` | server → client (401) | machine-readable "you need a token" |
