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
  Tokens live ~60–90 minutes and are fetched per connection; nothing is
  stored on disk by our scripts (the Azure CLI keeps its own token cache).
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

## Key Vault

Not created. The design has no secrets: storage via managed identity, ACR via
managed identity, CI via OIDC, clients via Entra tokens. A Key Vault
(~$0.03 per 10k operations, no base fee) becomes necessary only for the
future OAuth proxy's client secret (see `docs/connect-chatgpt.md`).

## Known limits (be explicit)

- Single-user v1. Workspace isolation exists in the data model and is tested,
  but role management is a config map, not an admin UI.
- The agent label is self-declared.
- Storage and ACR endpoints are public (Entra-only).
- Search is keyword substring matching, not a security boundary.
- No rate limiting beyond ACA scaling limits (max 2 replicas) and body caps.
- Memory bodies are stored in plain text in Azure Storage (encrypted at rest by
  Azure with Microsoft-managed keys). Do not store secrets there (rule 5).
