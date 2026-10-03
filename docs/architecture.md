# Architecture (v1)

## Goal

One shared, durable working state for several AI agents used by **one person**:
Claude Code, Codex, later Claude Desktop and ChatGPT. Rules and skills are
code (Git); working state (memories, decisions, tasks, messages, project cards)
lives in Azure and is reached through a remote MCP server with typed CRUD tools.

## Components

```
  Laptop                                   Azure (one resource group, eastus)
  ──────                                   ───────────────────────────────────
  Git clone C:\AI\collab ── push ──► GitHub (private): code, rules, skills, IaC
    shared/RULES.md, skills/                 │ Actions: ci (lint/test/scan)
    AGENTS.md / CLAUDE.md                    │          deploy (manual, OIDC)
                                             ▼
  Claude Code ─┐                         ACR (Basic) ── AcrPull (UAMI)
  Codex CLI  ──┤ HTTPS + Entra JWT          │
               │ (az login token)           ▼
               └──────────────────► Container App "collab-hub" (consumption,
                                     scale 0..2, /mcp Streamable HTTP, /healthz)
                                         │ user-assigned managed identity
                                         ├── Table "hub"  (Storage Table Data Contributor, table scope)
                                         └── Blob "content" (Storage Blob Data Contributor, container scope)
                                     Log Analytics (30 d, daily cap)
  Entra ID: API app "ai-collab-hub-api" (scopes Collab.Read / Collab.ReadWrite,
            assignment required) ── issues tokens to Azure CLI for the user
```

Terraform state is in a **separate** storage account (bootstrap stack) with
Entra-only access, versioning, soft delete and a delete lock.

## Request flow

1. The client gets headers from `scripts/mcp-headers.ps1`:
   `az account get-access-token --scope api://<api>/Collab.ReadWrite` →
   `Authorization: Bearer <JWT>` plus `X-Collab-Agent: claude-code|codex`.
2. ACA ingress terminates TLS and forwards to uvicorn :8000.
3. The SDK's bearer middleware calls our `JwtTokenVerifier`: RS256 signature
   against Entra JWKS, `iss`, `aud` (= API client ID), `exp`, `tid`, a collab
   scope in `scp`, and the `oid` must be listed in `COLLAB_PRINCIPALS`.
   Anything else → 401 with `WWW-Authenticate: resource_metadata=...`.
4. DNS-rebinding protection checks `Host`/`Origin`; bodies over 256 KiB → 413.
5. The tool resolves a `Principal` (alias, workspace, role, agent) and the
   service enforces read/write, workspace isolation and task-claimant rules.
6. Storage calls go out with the managed identity; results are typed
   (pydantic) and returned as structured content.

## Data model

One Azure Table (`hub`) and one Blob container (`content`).

| Row | PartitionKey | RowKey | Notes |
|---|---|---|---|
| record | `<workspace>~<kind>` | `r:<inverted ULID>` (project: `r:<slug>`) | inverted ⇒ newest first |
| revision | same | `v:<id>:<rev 8 digits>` | immutable snapshot of metadata + actor + change |
| idempotency | same | `i:<sha256(alias, tool, key)>` | request hash to detect key reuse |

Kinds: `project`, `memory`, `decision`, `task`, `message`.

- **Atomicity.** Record + revision (+ idempotency row, + superseded decision)
  are written in one entity-group transaction (same partition): all or
  nothing.
- **Concurrency.** Every update is `If-Match: <ETag>`; `expected_revision` is
  checked first for a clear error. `task_claim` relies on the ETag: of N
  concurrent claims exactly one commits, the rest get `conflict` (tested with
  4 parallel callers).
- **Bodies.** Memory bodies (up to 100k chars) go to
  `content/<workspace>/memory/<id>/<rev>.md`, uploaded *before* the
  transaction with `overwrite=False`, SHA-256 stored in the row and verified
  on read. If the transaction fails the blob is deleted; if that also fails the
  orphan is unreferenced (never visible) and expires with versions policy.
  Tasks, messages and decisions are small and stored inline in the row.
- **History.** `memory_get(revision=n)` and `include_history=true` read
  revision rows; old bodies remain as separate immutable blobs. Blob
  versioning and soft delete add a second safety net.

## Search (honest limits)

Azure Table has no full-text search or `contains` operator. v1 search is:

- server-side OData filter on metadata (partition, `project`, `status`,
  `verified`, `to_agent`, …);
- then an in-process check that **every** query word appears as a substring
  in a normalised text field (casefolded, NFKC, punctuation removed) and that
  all requested tags are present;
- newest first, bounded to **500 rows scanned per call**; the cursor resumes
  exactly where the scan stopped, so a page can contain fewer than `limit`
  items while `next_cursor` is set.

No ranking, stemming or fuzzy matching. Cost is one Table query page per 100
rows scanned (≈ $0.0004 per 10k transactions class; negligible at personal
scale). Beyond ~10k records per kind, or for semantic search, move the index
to Azure AI Search (free tier) — a later phase.

## Scale to zero

`min_replicas = 0`: no charge while idle. The first call after idle waits for
a cold start (a few seconds; the startup probe allows 60 s). The server is
stateless (`stateless_http=True`, JSON responses), so any replica can serve
any request and clients do not depend on sticky sessions.

## Two ways in: CLI clients and connectors

- **Claude Code, Codex** (on the laptop): Entra access token from the Azure CLI,
  served by `scripts/mcp-headers.cmd` from a local cache that a scheduled task
  refreshes (both clients kill header helpers after 10 s).
- **ChatGPT, Claude.ai / Desktop** (vendor clouds): the hub's own OAuth server —
  DCR, PKCE, Entra sign-in through the `ai-collab-hub-login` app (managed-identity
  credential), consent page, opaque hub tokens. Details:
  `docs/connect-chatgpt.md`, controls: `docs/security.md`.

Both end in the same `Principal` (verified Entra `oid` → alias/workspace/role)
and the same per-tool authorization.

## Deliberately not in v1

- No shell, command execution, file paths, GitHub token proxy or access from
  Azure to the laptop. Tools are typed CRUD only.
- No agent-to-agent auto-invocation: messages and tasks never trigger work.
- No private networking (would need a VNet-integrated environment).
- No per-agent Entra identity, no bulk export/backup tool, no semantic search.
