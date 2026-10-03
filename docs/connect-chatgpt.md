# Connecting ChatGPT and Claude.ai / Claude Desktop

These clients connect from their vendors' clouds and authenticate with OAuth
(Dynamic Client Registration or CIMD, PKCE, RFC 8707 `resource`). Microsoft
Entra ID supports neither DCR nor CIMD, so the hub runs its **own OAuth
authorization server** (`COLLAB_OAUTH_ENABLED=true`, Terraform
`enable_oauth = true`, the default) and delegates user sign-in to Entra.

## How it works

```
ChatGPT / Claude.ai                     Hub (https://<fqdn>)                   Entra ID
  │ GET /mcp → 401 + resource_metadata    │
  │ GET /.well-known/oauth-protected-resource/mcp → authorization_servers=[hub]
  │ GET /.well-known/oauth-authorization-server
  │ POST /register (DCR; redirect URI must be on the allowlist)
  │ GET /authorize (PKCE S256, resource=<fqdn>/mcp) ──► redirect ───────────► sign-in (login app,
  │                                       │                                   assignment required)
  │                                       │ ◄── /oauth/callback?code ─────────┘
  │                                       │ redeem code (PKCE + managed-identity client assertion)
  │                                       │ oid must be in COLLAB_PRINCIPALS
  │ ◄── consent page (once per user + client): "Allow <app> to read and write…?"
  │ ◄── 302 redirect_uri?code=…&state=…&iss=<hub>/
  │ POST /token (code + code_verifier) ──► opaque access token (1 h) + refresh token (30 d, rotating)
  │ POST /mcp  Authorization: Bearer chb_at_… → tools
```

- Allowed redirect URIs (exact scheme/host, see `COLLAB_OAUTH_REDIRECT_ALLOWLIST`):
  `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`,
  `https://chatgpt.com/connector_platform_oauth_redirect`,
  `https://chatgpt.com/connector/oauth/*`, and `http://localhost:*` /
  `http://127.0.0.1:*` for local CLI clients.
- Records written through a connector are attributed to `owner/chatgpt` or
  `owner/claude-desktop` (derived from the client's redirect host).
- Tokens and codes are 256-bit random values stored only as SHA-256 hashes in
  the hub's table. Codes and refresh tokens are single use (atomic ETag claim);
  a refresh never widens scopes or extends the 30-day absolute lifetime.
- The login app authenticates to Entra with the Container App's managed
  identity (federated identity credential). There is no client secret and no
  Key Vault.
- Claude Code and Codex keep using Entra tokens from the Azure CLI; both token
  kinds are accepted.

## Connect Claude (claude.ai, Claude Desktop, Claude mobile)

Connectors are configured once on claude.ai and appear in Claude Desktop and
mobile. Requires Pro/Max (Team/Enterprise: an owner adds it).

1. claude.ai → **Settings → Connectors → Add custom connector**.
2. Name `AI Collab Hub`, URL `https://<fqdn>/mcp` (Terraform output `mcp_url`).
   Leave the OAuth client ID/secret fields empty (DCR is used).
3. **Connect** → Microsoft sign-in (your account) → hub consent page → **Allow**.
4. In a chat, enable the connector and ask: `Call project_list from AI Collab Hub`.

## Connect ChatGPT

Developer mode is documented for Pro, Plus, Business, Enterprise and Edu **on
the web**; whether write tools are allowed on your plan is shown by ChatGPT
itself.

1. chatgpt.com → **Settings → Apps & Connectors → Advanced settings →
   Developer mode** (on).
2. **Create / Add custom connector**: name `AI Collab Hub`, MCP server URL
   `https://<fqdn>/mcp`, authentication **OAuth** (no client ID: DCR).
3. Sign in with Microsoft → hub consent page → **Allow**.
4. In a new chat, add the connector and ask: `Call project_list`.
   Write tools ask for confirmation in ChatGPT.

## Troubleshooting

- *"redirect_uri is not on this server's allowlist"*: the client uses a new
  callback URL; add it to `COLLAB_OAUTH_REDIRECT_ALLOWLIST` (Terraform env)
  after checking it belongs to the vendor.
- *access_denied / "this account is not allowed"*: the Microsoft account you
  signed in with is not in `collab_principals` or not assigned to the
  `ai-collab-hub-login` enterprise app.
- Revoke a connector: remove it in the client; its tokens expire (access 1 h,
  refresh 30 d). To cut everything immediately, delete the `oauth~*` rows in the
  `hub` table (Storage Browser) or set `enable_oauth = false` and apply.

## Not implemented

- CIMD (Client ID Metadata Documents): the server does not advertise it, so
  clients fall back to DCR. DCR is deprecated in the MCP spec but still
  supported by ChatGPT and Claude.
- OpenAI Secure MCP Tunnel: not needed (the server is public).
