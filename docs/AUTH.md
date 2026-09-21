# Auth & trust model for the HTTP transport

The stdio server never needed auth: the OS *is* the authentication. A host
spawns the server as a child process, so only something with filesystem
access could talk to it — and anything with filesystem access could open the
SQLite file directly. HTTP changes that completely: the moment the server
listens on a socket, "who is allowed to call this" becomes a real question.

## The scheme: one shared bearer token

When `MCP_TRANSPORT=http` is set, the server **refuses to start** unless
`MCP_AUTH_TOKEN` is set (fail-closed). Every `/mcp` request must carry
`Authorization: Bearer <token>`; anything else gets a protocol-conformant
`401` with a JSON-RPC `error` object and a `WWW-Authenticate: Bearer` header
(no body parsing, no DB access, no work done — auth happens before the MCP
layer sees anything). The token is compared with `hmac.compare_digest`, and
no `Authorization` header means no token is echoed in any error message.

Two operational notes worth writing down:

- **Timing attacks**: comparing secrets with `==` leaks length/prefix info
  through response-time differences. `hmac.compare_digest` is the standard
  fix; the token is only ever compared, never logged.
- **The token is per-installation, not per-user**: one secret in an env var
  stands in for real identity. That is exactly the right strength for a
  demo server and exactly wrong for anything multi-tenant.

## What this design deliberately does NOT claim

1. **It does not fix spoofable attribution.** `client_id` remains a plain
   tool argument; a caller can claim `client_id="claude-desktop"` and poison
   the audit trail *even with a valid token*. The threat model stays
   "debugging aid, not security" over HTTP too. Honest boundaries:
   - what a token buys: only token-holders can write at all;
   - what it doesn't buy: proof of *who* wrote — attribution is still
     self-reported.
2. **No per-client identity, no revocation, no scopes.** One shared secret
   means "rotate it" is the only response to a leak. A real design would
   mint per-client tokens server-side and have the server *assign* the
   identity (making attribution trustworthy); the natural evolution here is
   `MCP_AUTH_TOKEN_<CLIENT_ID>` env vars.
3. **Transport is plaintext HTTP locally.** Fine for localhost and for a
   Cloudflare-Tunnel deployment (TLS terminates at the edge); would not be
   fine on an open network.

## Why not the SDK's OAuth machinery

mcp 2.x ships full OAuth 2.1 resource-server support (token verifiers,
protected-resource metadata, an auth server provider). It is the right tool
for a public, multi-tenant deployment — and roughly 10× the code and moving
parts this project needs. A shared bearer token plus the SDK's built-in
DNS-rebinding protection is the honest 90% for a single-operator demo, and
`docs/NOTES.md` is where the rationale lives rather than pretending the
simple scheme is something bigger.

## Header walkthrough (what each one is for)

| Header | Direction | Purpose |
|---|---|---|
| `Authorization: Bearer <t>` | client → server | the credential itself |
| `Accept: application/json` | client → server | requests single JSON responses (no SSE stream) |
| `Content-Type: application/json` | client → server | framing on the way in |
| `Mcp-Session-Id` | server → client, then echoed back | names the server-side session created at `initialize` |
| `MCP-Protocol-Version` | client → server on later calls | tells the server which negotiated version is in use |
| `WWW-Authenticate: Bearer` | server → client (401) | machine-readable "you need a token" |
