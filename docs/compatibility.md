# Compatibility (verified 2026-09-28)

Everything below was checked on the date above against official pages,
package registries or by running the code. Items marked **UNVERIFIED** could
not be confirmed and must not be relied on. Re-check before changing auth or
client setup; these products change quickly.

## Versions used

| Component | Version | How verified |
|---|---|---|
| MCP specification | `2026-07-28` (stateless; `server/discover`), backward-compatible with `2025-11-25` handshake | https://modelcontextprotocol.io/specification/versioning |
| Python MCP SDK `mcp` | 2.2.0 (2026-09-07); FastMCP is now `mcp.server.mcpserver.MCPServer` | https://pypi.org/project/mcp/ , https://py.sdk.modelcontextprotocol.io/migration/ ; installed and tested |
| Terraform | 1.16.4 latest; repo requires `>= 1.9`, tested with 1.15.4 | api.releases.hashicorp.com; `terraform validate` |
| azurerm provider | 5.7.0 (2026-09-24) | registry.terraform.io; lock file |
| azuread provider | 3.10.0 (2026-09-24) | registry.terraform.io; lock file |
| Azurite | 3.37.0 (Blob + Table (preview)) | https://github.com/Azure/Azurite ; test suite runs on it |
| Python | 3.13 in the container, 3.12+ supported, tests run on 3.14 locally | CI + local |

Our test suite connects with the official client in both modes:
`mode="auto"` (2026-07-28 discovery) and `mode="legacy"` (initialize
handshake used by older clients).

## MCP authorization (spec 2026-07-28)

Source: https://modelcontextprotocol.io/specification/latest/basic/authorization

- Servers MUST publish RFC 9728 Protected Resource Metadata and answer 401
  with `WWW-Authenticate: Bearer resource_metadata="..."`. **Implemented** by
  the SDK: `/.well-known/oauth-protected-resource/mcp` (tested).
- Clients MUST send the RFC 8707 `resource` parameter; servers MUST validate
  that tokens were issued for them and MUST NOT pass tokens through. We check
  `aud` = the API app's client ID (Entra v2 behaviour) and never forward tokens.
- Client registration: Client ID Metadata Documents (CIMD, SHOULD),
  pre-registration, or Dynamic Client Registration (DCR, now **deprecated**, MAY).

## Microsoft Entra ID as the authorization server

| Question | Finding | Source |
|---|---|---|
| DCR | Not supported | https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-mcp |
| CIMD | Not supported (community post, not official docs) | techcommunity.microsoft.com blog 4508453 |
| RFC 8707 `resource` | Accepted only if the exact MCP URL is registered as an Application ID URI, which Entra allows only on a verified custom domain; otherwise `AADSTS9010010` | https://claude.com/docs/connectors/building/troubleshooting |
| v2 token `aud` | Always the API's client ID (GUID) | https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference |
| `requestedAccessTokenVersion` | null = v1; we set 2 | app manifest reference (updated 2026-09-25) |
| `az account get-access-token --scope` | v2 scopes; used by our header helper | https://learn.microsoft.com/en-us/cli/azure/account |
| Azure CLI as a pre-authorized client (`04b07795-...`) | **UNVERIFIED** in docs; configured in bootstrap, confirm on first token request | — |

**Consequence:** plain Entra cannot complete the interactive OAuth flow that
Claude.ai / Claude Desktop connectors and ChatGPT perform (they need CIMD or
DCR, or a pre-registered client plus a `resource` value Entra accepts). The v1
design therefore uses Entra as issuer and the **Azure CLI login** as the token
source for Claude Code and Codex (see `docs/security.md`). A DCR/CIMD-capable
proxy is the documented next step (see `docs/connect-chatgpt.md`).

## Claude Code

Source: https://code.claude.com/docs/en/mcp , /memory , /skills

- `claude mcp add --transport http [--scope local|project|user] <name> <url>`;
  project scope is `.mcp.json` in the repo root; local/user in `~/.claude.json`.
- `headers` with `${VAR}` expansion and **`headersHelper`** (a command that
  prints a JSON object of headers; runs on each connect, 10 s timeout; project
  scope requires trust approval). **Used by `scripts/connect-claude.ps1`.**
- OAuth: DCR and CIMD automatically; pre-registered client via `--client-id`,
  `--client-secret`, `--callback-port` (redirect `http://localhost:PORT/callback`).
- `CLAUDE.md` imports: `@path` relative to the importing file, up to 4 hops.
- Skills: `.claude/skills/<name>/SKILL.md` (project), `~/.claude/skills/...`
  (personal).

## Claude Desktop (chat) vs Claude Code

Source: https://support.claude.com/en/articles/11175166 ,
https://support.claude.com/en/articles/10949351 ,
https://claude.com/docs/connectors/building/authentication

- Claude Desktop **chat** uses *Connectors* configured in claude.ai settings
  (Customize > Connectors on Pro/Max). Connections originate from Anthropic's
  cloud (`160.79.104.0/21`), not from your PC. Auth: none, OAuth DCR, OAuth
  CIMD, or a customer-entered client ID/secret; callback
  `https://claude.ai/api/mcp/auth_callback`. Anthropic lists auth specs
  2025-03-26 / 2025-06-18 / 2025-11-25.
- `claude_desktop_config.json` configures **local stdio** servers only; it is
  separate from Connectors and from Claude Code's `.mcp.json` / `~/.claude.json`.
- **Status (2026-10-03):** supported through the hub's own OAuth server (DCR;
  Entra sign-in behind it). Redirect URIs on the allowlist:
  `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`.

## Codex CLI / App

Source: https://learn.chatgpt.com/docs/extend/mcp (redirect from
developers.openai.com/codex/mcp), /docs/build-skills.md,
/docs/agent-configuration/agents-md.md

- `~/.codex/config.toml`; project `.codex/config.toml` is read **only for
  trusted projects**.
- `[mcp_servers.<name>]`: `url`, `auth`, `bearer_token_env_var`,
  `http_headers`, `env_http_headers`, **`http_headers_helper`** (a string
  command that prints a JSON object of headers; cached per connection,
  refreshed once after a 401/403; explicit bearer tokens and OAuth take
  precedence over a helper `Authorization`), `startup_timeout_sec`,
  `enabled_tools`, `disabled_tools`. **Used by `scripts/connect-codex.ps1`**
  (`http_headers_helper` → `scripts/mcp-headers.cmd codex`). Checked
  2026-10-01. Observed in Codex Desktop 26.917 logs: the helper is killed
  after **10 seconds** ("MCP HTTP headers helper timed out after 10 seconds"),
  same limit as Claude Code, hence the cached `.cmd` helper.
- OAuth: `codex mcp add ... --oauth-client-id` (static client),
  `codex mcp login` (CIMD, DCR fallback).
- AGENTS.md: concatenated from git root down to cwd, 32 KiB default cap; no
  import syntax, so our `AGENTS.md` tells Codex which files to read.
- Skills: `.agents/skills` (repo, cwd up to root), `$HOME/.agents/skills`;
  `SKILL.md` requires `name` and `description`.
- The ChatGPT desktop app's Codex uses `~/.codex/config.toml` and has
  Settings > MCP servers > Add server (docs, checked 2026-10-01; also seen
  locally: the app writes `[projects.'<path>'] trust_level = "trusted"` there).
- The `codex` CLI is not on PATH on the build machine; the app is used instead.

## ChatGPT

Source: https://developers.openai.com/api/docs/guides/developer-mode ,
https://developers.openai.com/plugins/build/auth ,
https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

- Developer mode (custom MCP): "Pro, Plus, Business, Enterprise, and Education
  accounts **on the web**". Desktop/mobile: **UNVERIFIED**.
- Write actions supported with confirmation; per-plan write limits:
  **UNVERIFIED** (help center page returned 403). Do not assume Plus has write.
- Auth: OAuth (CIMD preferred, then DCR, then predefined static client) or no
  auth. Redirect `https://chatgpt.com/connector_platform_oauth_redirect`.
  ChatGPT sends `resource` and expects it in `aud`.
- **Secure MCP Tunnel** exists (outbound-only `tunnel-client` polling
  `api.openai.com:443`; works with developer mode, Codex, Responses API).
  Plan availability and GA status: **UNVERIFIED**. It solves *reachability* of
  private servers, not authentication; our server is already public, so it adds
  nothing for v1.
- **Status (2026-10-03):** supported through the hub's own OAuth server (DCR,
  PKCE, `resource`, RFC 9207 `iss`). Both ChatGPT redirect forms are on the
  allowlist. Plan-specific limits (write tools) are decided by ChatGPT. See
  `docs/connect-chatgpt.md`.

## Azure Container Apps and Storage in Terraform (azurerm 5.x)

Source: provider docs + `website/docs/guides/5.0-upgrade-guide` in
github.com/hashicorp/terraform-provider-azurerm

- `azurerm_storage_container` / `azurerm_storage_table` take
  `storage_account_id` (management plane) — used.
- `azurerm_container_app_environment` needs `logs_destination = "log-analytics"`
  with `log_analytics_workspace_id` — used.
- `min_tls_version` accepts only `TLS1_2`; `allow_nested_items_to_be_public`
  defaults to false; `public_network_access_enabled` deprecated in favour of
  `public_network_access` — used.
- `resource_provider_registrations = "none"` (5.x default); the subscription
  already has Microsoft.App, OperationalInsights, Storage, ContainerRegistry,
  ManagedIdentity registered (checked with `az provider show`).
- Container App: `registry { identity = <UAMI id> }`, HTTP probes, `min_replicas = 0`
  (scale to zero is confirmed on the pricing page).
- Backend `azurerm` with `use_azuread_auth = true` needs Storage Blob Data
  Contributor; locking uses blob leases.
- Microsoft's own guidance for MCP on Container Apps (built-in auth + bearer
  token, stateless server, separate `/healthz`):
  https://learn.microsoft.com/en-us/azure/container-apps/mcp-authentication
  We validate tokens in the app instead of Easy Auth so the RFC 9728 metadata
  and per-tool authorization stay in one place and are testable locally.
