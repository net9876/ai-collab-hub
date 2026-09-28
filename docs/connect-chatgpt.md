# Connecting ChatGPT (not available in v1)

**Status: not connected.** The rest of the system works without it.

## Why not

ChatGPT developer mode connects to custom MCP servers from OpenAI's cloud and
authenticates with OAuth, preferring Client ID Metadata Documents (CIMD), then
Dynamic Client Registration (DCR), then a predefined static client. It sends
the RFC 8707 `resource` parameter and expects that value as the token
audience. Sources and dates: `docs/compatibility.md`.

Our authorization server is Microsoft Entra ID, which:

- supports neither DCR nor CIMD;
- accepts the `resource` value only when the exact MCP URL is registered as an
  Application ID URI, which requires a **verified custom domain** (the default
  `*.azurecontainerapps.io` host cannot be used);
- issues v2 tokens whose `aud` is the API client ID (GUID), not the MCP URL.

We will not add an unauthenticated or shared-secret mode to make it work.

Plan limits: developer mode is documented for Pro, Plus, Business, Enterprise
and Education **on the web**; write-action limits per plan could not be
verified. Your plan and whether write tools are allowed must be checked in
your own ChatGPT settings before investing in the steps below.

## Path to connect later (in order of preference)

1. **OAuth proxy in front of Entra** (recommended). An authorization-server
   facade that offers CIMD/DCR to ChatGPT and Claude.ai and delegates user
   sign-in to Entra with a pre-registered confidential client. The standalone
   `fastmcp` package (4.x) ships an `AzureProvider` doing exactly this
   (needs client ID, **client secret**, tenant ID, base URL, scopes).
   Work needed:
   - a second Entra app (web platform) with redirect URIs
     `https://chatgpt.com/connector_platform_oauth_redirect` and
     `https://claude.ai/api/mcp/auth_callback`;
   - a Key Vault (or ACA secret backed by Key Vault) for the client secret;
   - persistent storage for the proxy's client registrations and tokens
     (Table Storage works);
   - map proxy-issued tokens to the same `COLLAB_PRINCIPALS` allowlist;
   - tests for the full authorization-code + PKCE flow.
   Cost impact: ~$0 (Key Vault operations are cents).
2. **Custom domain + pre-registered client.** Add a verified domain to the
   tenant, map it to the Container App, register `https://<domain>/mcp` as an
   Application ID URI, and enter a static client ID in ChatGPT/Claude.ai.
   Depends on whether the client's `resource` handling then matches Entra;
   verify before building.
3. **Secure MCP Tunnel** (OpenAI) solves reachability for private servers. It
   does not replace authentication and is not needed while the server is
   public.

## When done, verify

- ChatGPT → Settings → Connectors → Advanced → Developer mode → add
  `https://<fqdn>/mcp`; sign in; list tools; call `project_list`.
- Confirm write tools prompt for confirmation, and that a user not in
  `COLLAB_PRINCIPALS` is refused.
- Record the result (and the plan it was tested on) in `docs/compatibility.md`.
