# AI Collab Hub

Shared working state for several AI agents of one person — **Claude Code**,
**Codex**, later **Claude Desktop** and **ChatGPT** — through a remote MCP
server on Azure Container Apps, backed by Azure Table + Blob Storage and
protected by Microsoft Entra ID.

- **Git (this repo)** holds rules, skills, schemas, code and infrastructure.
- **The hub** holds working state: memories, decisions, tasks, messages and
  project cards — each with an ID, UTC timestamps, a verified author, a
  revision and history.
- Agents get **typed CRUD tools only**. Nothing in the hub can run commands,
  and messages or tasks never make another agent act on their own.

> **Status: v1, single user, deployed** (Azure Container Apps, East US).
> Claude Code and Codex connect with Entra tokens from the Azure CLI; ChatGPT
> and Claude.ai/Desktop connect through the hub's own OAuth server (Entra
> sign-in). See `docs/STATUS.md` for the current state and open items.

## Layout

```
AGENTS.md, CLAUDE.md        client bootstrap -> shared/RULES.md
shared/                     RULES, PROJECTS, DECISIONS, profile/preference templates
skills/                     azure-architecture, terraform-review, research (SKILL.md)
server/                     Python MCP server (collab_hub) + tests
infra/bootstrap/            state backend, Entra API app, GitHub OIDC identity
infra/terraform/            Storage, ACR, Log Analytics, Container Apps, RBAC
scripts/                    setup, validate, connect-claude, connect-codex, smoke
docs/                       architecture, api, security, costs, compatibility, runbook
.github/workflows/          ci (lint/test/scan/build), deploy (manual, OIDC)
```

## Quick start (local, no Azure changes)

```powershell
pwsh -File scripts/setup.ps1       # Windows PowerShell 5.1: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
pwsh -File scripts/validate.ps1
```

`validate.ps1` runs ruff, mypy, the pytest suite (Azurite Blob+Table emulator
and a real MCP client over HTTP: handshake, tools/list, tools/call, auth
denial, pagination, concurrent task claims, storage failures), Terraform
fmt/validate, checkov and a secret/private-ID scan.

## Deploy and connect

Follow `docs/runbook.md`: bootstrap → main stack (two steps, because the
Container App needs an image in the registry the stack creates) → smoke test →
`scripts/connect-claude.ps1` / `scripts/connect-codex.ps1`. Estimated cost
≈ $5–7/month, mostly ACR Basic (`docs/costs.md`).

## Client support

| Client | v1 | How |
|---|---|---|
| Claude Code | yes | `.mcp.json` HTTP server + `headersHelper` getting an Entra token from `az` |
| Codex (ChatGPT desktop app or CLI) | configured | project `.codex/config.toml` with `http_headers_helper` (same `az` token helper); open the repo as a trusted project |
| Claude.ai / Claude Desktop | yes (custom connector) | hub OAuth server: DCR + PKCE, Entra sign-in, consent page; `docs/connect-chatgpt.md` |
| ChatGPT (developer mode, web) | yes (custom connector) | same OAuth server; write tools need ChatGPT's confirmation |

## Limits worth knowing

- Tokens come from your Azure CLI login and last ~60–90 minutes; Claude Code
  and Codex both re-run the header helper on reconnect.
- The agent label (`X-Collab-Agent`) is self-declared; the user identity is
  verified.
- Search is keyword/metadata matching over Azure Table, not full-text search.
- Storage and registry endpoints are public (Entra-only access, keys disabled).

Details: `docs/security.md`, `docs/architecture.md`, `docs/compatibility.md`.
Continuing work across clients (sessions, checkpoints, task handoff): `docs/workflow.md`;
next phases: `docs/roadmap.md`.
