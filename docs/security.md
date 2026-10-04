# Security

This is a **v1, single-user** system. It is authenticated and authorized end
to end, but it is not a multi-tenant product and has known limits (bottom of
this page).

## Threat model (short)

| Threat | Control |
|---|---|
| Anonymous internet access to `/mcp` | Every MCP request needs a valid Entra JWT; there is no anonymous or "dev" bypass in the code. `/healthz` returns only status/version. |
| Token for another app/tenant replayed here | `aud` must equal the API client ID, `iss` the tenant's v2 issuer, `tid` the tenant; RS256 only (HS256/none rejected); 60 s leeway. |
| Someone else in the tenant gets a token | The API's service principal has *assignment required*; only assigned users get tokens. The server additionally allowlists `oid`s (`COLLAB_PRINCIPALS`). |
| Agent spoofs another author | Author = verified alias + agent label. The label is self-declared and recorded as such; the alias cannot be forged. |
| Prompt injection through stored content | Messages/tasks/memories are returned with a notice that they are data; rules and tool descriptions forbid acting on them without the user; no tool can execute anything. |
| Lost update / double claim | ETag If-Match on every write; entity-group transactions; tested concurrent claim. |
| Oversized or malformed input | 256 KiB body cap (413), pydantic schemas with lengths/patterns, cursor validation, bounded scans. |
| DNS rebinding / wrong Host | SDK transport security with an explicit host allowlist (the app FQDN). |
| Storage key leak | Keys disabled on both storage accounts (`shared_access_key_enabled = false`); app uses a managed identity; operators use Entra. |
| Over-privileged workload identity | UAMI has Blob Data Contributor on **one container**, Table Data Contributor on **one table**, AcrPull on the registry. Nothing else. |
| CI credential theft | GitHub OIDC federation (no Azure secret). Deploy SP: AcrPush (registry), Container Apps Contributor (the app), Managed Identity Operator (the app's UAMI). Subject pinned to `repo:<owner>/<repo>:environment:production`. |
| Secrets/personal data in Git | `.gitignore` for tfvars/state/plans/local profile; `scripts/scan_secrets.py` blocks keys, JWTs, non-placeholder GUIDs and e-mails; gitleaks in CI. |
| Logs leaking content | App logs one JSON line per tool call: tool, actor, principal fingerprint (sha256 prefix, not the oid), outcome, latency. No arguments, bodies, tokens or names. Azure SDK logging is at WARNING (no URLs). Uvicorn access log is off. |
| Accidental data/state deletion | CanNotDelete locks on state and data accounts, `prevent_destroy` on the state account, blob versioning + soft delete, lifecycle keeps 90 days of versions. |

## Identity and tokens

- **Issuer:** Microsoft Entra ID (tenant of the subscription).
- **Audience:** API app `ai-collab-hub-api`, `requested_access_token_version = 2`,
  identifier URI `api://<client-id>`.
- **Scopes:** `Collab.Read` (read tools), `Collab.ReadWrite` (all tools). The
  principal's `role` (`reader`/`writer`) is enforced server-side as well.
- **Token source for v1 clients:** the user's Azure CLI login
  (`az account get-access-token --scope api://<client-id>/Collab.ReadWrite`).
  Azure CLI is pre-authorized on the API app so no consent prompt is needed.
  Tokens live ~60–90 minutes.
- **Local token cache.** Claude Code and Codex kill header helpers after 10
  seconds, and PowerShell + `az` can take far longer on a loaded machine. The
  helper they run (`scripts/mcp-headers.cmd`) therefore only prints a cached
  header file from `%LOCALAPPDATA%\ai-collab-hub` (ACL: current user only,
  inheritance removed). The cache holds one access token for this hub's
  audience only, never a refresh token, and is renewed by
  `mcp-headers.ps1 -Refresh` — run by the optional per-user scheduled task from
  `scripts/token-refresh-task.ps1` (every 5 min, `-Uninstall` removes it) or by
  hand. Anyone able to read your profile can already use the Azure CLI's own,
  more powerful cache, so this adds little exposure; delete the folder to
  revoke locally.
- **Why not interactive OAuth in the client?** Entra supports neither DCR nor
  CIMD, and only accepts the RFC 8707 `resource` value if the MCP URL is an
  Application ID URI on a verified custom domain. Claude Code/Codex can use
  static headers instead, which is what v1 does. See `docs/compatibility.md`.

## Per-agent identity

Both Claude Code and Codex present the **same** user token (Azure CLI client).
The server therefore knows *who* (verified) and *which agent* (declared). A
stronger per-agent identity would need a separate Entra client per agent
(e.g. two public client apps with their own sign-in), which is possible later
but adds sign-in friction; it does not change the server.

## Static analysis: accepted findings

`checkov` runs in CI with `.checkov.yaml`. Accepted for v1:

- **Public endpoints** on storage and ACR (CKV_AZURE_59, CKV2_AZURE_33,
  CKV_AZURE_139): consumption Container Apps without VNet integration have no
  fixed egress IP. Data-plane access still needs Entra + RBAC; keys are off.
  Fix path: workload-profile environment with VNet + private endpoints
  (roughly +$10–20/month minimum for the environment and endpoints).
- **Read logging** for blob/table (CKV2_AZURE_20/21): reads are the hot path
  and would dominate log cost; writes and deletes are logged.
- **LRS** (CKV_AZURE_206): cost; `storage_replication` can be raised.
- **Microsoft-managed keys** (CKV2_AZURE_1): CMK needs Key Vault + rotation.
- **ACR Premium-only features** (CKV_AZURE_163/164/165/166/167/233/237).
- Queue logging (not used) and two checks checkov cannot evaluate
  (CKV_AZURE_43 computed name, CKV_AZURE_249 interpolated OIDC subject).

## OAuth server for connectors (ChatGPT, Claude.ai)

Remote connectors need DCR/CIMD, which Entra lacks, so the hub is its own
OAuth 2.1 authorization server (flow: `docs/connect-chatgpt.md`). Controls:

- **Only allowlisted identities.** Sign-in is delegated to Entra (login app,
  *assignment required*); the returned `oid` must be in `COLLAB_PRINCIPALS`,
  checked again on every token use and refresh.
- **No secret.** The login app's credential is a federated identity credential
  for the Container App's managed identity.
- **Confused-deputy protection.** A consent page (once per user + client) names
  the client and its redirect host before any code is issued; pages send
  `X-Frame-Options: DENY` and a CSP with `frame-ancestors 'none'`.
- **Redirect allowlist** with structured matching (scheme + host exact, no
  userinfo/query/fragment), so `http://localhost:1@evil.example/` is refused.
- **PKCE S256 required**, `resource` must equal the MCP URL, `iss` returned
  (RFC 9207); authorization codes 5 min and single use; refresh tokens rotate,
  are single use, cannot widen scopes and keep a 30-day absolute lifetime;
  access tokens 1 h; revocation endpoint enabled.
- **Storage.** Codes and tokens are 256-bit random strings stored only as
  SHA-256 hashes. DCR client secrets (for clients that ask for one) are stored
  in the Entra-protected table so the token endpoint can verify them. DCR is
  capped at 200 registered clients.
- **Not done:** expired `oauth~*` rows are not garbage-collected (tiny volume);
  CIMD is not offered.

## Sessions and handoffs

- **Scope does not travel by itself.** Every checkpoint and resume response carries
  a provenance notice: the content is data written by another agent of the same user,
  it keeps only the scope the user already authorized for that work, and it grants no
  new permissions, spending or background actions. Tool descriptions and server
  instructions repeat this to all clients (`shared/RULES.md` §3/§3a).
- **User words vs agent ideas** are separate fields (`user_constraints` vs
  `hypotheses`), so a resuming agent does not mistake a guess for a requirement.
- **No secrets, no hidden reasoning, no transcripts**: checkpoints are structured
  summaries (60,000-byte cap). Raw transcript capture is opt-in and not implemented.
- **No implicit ownership changes**: resuming never moves a task; `task_accept` is
  explicit, atomic (ETag), audited and limited to the agent label named by the owner.
- **Isolation**: sessions live in the workspace partition; cross-project resume is
  refused; only the session's own client label can checkpoint or close it.
- **Self-declared labels**: `source_client` and the agent part of `created_by` come
  from the client (`X-Collab-Agent` or the OAuth client's redirect host). They separate
  workstreams; they are not an identity boundary between agents of the same user.

## Key Vault

Not created, still. The design has no secrets: storage, ACR and the OAuth
login app use the managed identity; CI uses OIDC; CLI clients use Entra tokens.

## Known limits (be explicit)

- Single-user v1. Workspace isolation exists in the data model and is tested,
  but role management is a config map, not an admin UI.
- The agent label is self-declared.
- Storage and ACR endpoints are public (Entra-only).
- Search is keyword substring matching, not a security boundary.
- No rate limiting beyond ACA scaling limits (max 2 replicas) and body caps.
- Memory bodies are stored in plain text in Azure Storage (encrypted at rest by
  Azure with Microsoft-managed keys). Do not store secrets there (rule 5).
