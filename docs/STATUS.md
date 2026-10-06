# AI Collab Hub — project status

Snapshot of what is implemented, verified and still open. Details are in the
other files under `docs/`.

## 1. What it is

Shared working memory for several AI agents of one person. The agents are
separate programs that know nothing about each other; the hub gives them shared
state between sessions:

- **projects** — cards that records are filed under;
- **memory** — verified facts (`verified`) and hypotheses, Markdown body;
- **decisions** — ADR-style records; a new decision can atomically supersede an old one;
- **tasks** — with atomic claiming: of several agents, exactly one wins;
- **messages** — notes between agents, with a recipient and threads;
- **sessions** — checkpoints and task handoff between clients (`docs/workflow.md`).

Every record has an ID, a UTC time, a verified author (`owner/<agent>`), a
revision number and history. There are no silent overwrites: an update requires
`expected_revision`.

Rules for agents and skills live in Git (`shared/RULES.md`, `skills/`); working
state lives in the hub. Agents only get typed CRUD operations (29 tools, see
`docs/api.md`). The hub cannot run a command, read a file or reach the user's
machine. Messages and tasks are data, not orders: an agent must ask the user
before acting on them.

## 2. How it works

```
Workstation                                   Azure (one resource group)
Claude Code ─┐  Entra token from az            Container App (MCP, Python, scale 1..2)
Codex ───────┤  (cache + refresh task)   ──►    /mcp  /healthz  OAuth endpoints
             │                                  │ managed identity (no keys)
ChatGPT ─────┤  hub OAuth server (DCR+PKCE)     ├─ Table Storage: records, history, OAuth state
Claude.ai ───┘  sign-in through Entra           └─ Blob Storage: memory bodies
                                                ACR Basic, Log Analytics
GitHub: code, rules, CI, manual deploy (OIDC, no stored secrets)
Entra ID: API app, login app, GitHub deploy app
```

- **Server:** Python, official MCP SDK, Streamable HTTP, stateless.
- **Storage:** one table and one blob container. A record, its revision and its
  idempotency key are written in one transaction. Concurrency uses ETags.
  Search is keyword and metadata matching, not full-text.
- **Access for CLI agents** (Claude Code, Codex): an Entra token from
  `az login`. These clients wait at most 10 seconds for a header helper, while
  `az` can take 20–30 seconds on a slow machine, so `scripts/mcp-headers.cmd`
  returns the token from a local cache at once and a scheduled task refreshes
  the cache every 5 minutes. The refresh log is
  `%LOCALAPPDATA%\ai-collab-hub\refresh.log`.
- **Access for connectors** (ChatGPT, Claude.ai/Desktop): the hub is its own
  OAuth server. It registers the client (DCR), sends the user to Microsoft
  sign-in and shows a consent page, then issues its own 1-hour access token and
  30-day refresh token. The hub proves itself to Entra with its managed
  identity — no client secret and no Key Vault.
- **Who is allowed:** only the Entra accounts listed in `collab_principals`.
  The check runs at sign-in and on every token use.
- **Infrastructure:** Terraform, two stacks. `infra/bootstrap` — state storage,
  Entra apps, GitHub OIDC. `infra/terraform` — everything else.
- **Cost:** roughly $5–12 per month, mostly ACR Basic (~$5) plus the always-on
  replica. Storage costs cents; logs are capped at 0.2 GB per day
  (`docs/costs.md`).

## 3. Implemented and verified

| Area | Status | How it is verified |
|---|---|---|
| Server, 29 tools | done | 64 tests on Azurite through a real MCP client: auth denial, pagination, task race, storage failures, full OAuth flow |
| Sessions and task handoff | done | protocol-level tests of both handoff scenarios; see `docs/workflow.md` |
| Azure deployment | done | HTTPS smoke test: 401 without a token, calls with a token, task lifecycle |
| Claude Code | supported | `/mcp` shows `collab` connected; tool calls work |
| Codex | supported | project tools called from Codex |
| Token refresh | done | scheduled task calls `az` and refreshes the cache |
| Claude.ai / Claude Desktop | supported | custom connector through the hub OAuth server |
| ChatGPT | supported | custom connector; write tools need ChatGPT's confirmation |
| Agent messaging | done | Codex → Claude Code and back |
| GitHub deploy | done | `deploy` workflow: OIDC sign-in, build, roll out, smoke test |
| CI | done | ruff, mypy, pytest, terraform validate, checkov, gitleaks, docker build; runner pinned to ubuntu-24.04 |

Problems found and fixed during the real deployment (tests did not catch them):

- **azuread:** every plan tried to remove the API app's identifier URI.
- **Container Apps:** Azure now creates a default `Consumption` workload
  profile, and the plan tried to delete it.
- **10-second helper limit** of Claude Code and Codex.
- **GitHub OIDC:** the subject uses the new immutable-ID format,
  `repo:owner@id/repo@id:...`.
- **Table SDK:** deleting a missing row counts as success, so one-time codes use
  an ETag-guarded marker instead.
- **Entra id_token without `oid`:** the `profile` scope is required, not only `openid`.
- **Consent page:** CSP `form-action 'self'` blocked the redirect back to
  claude.ai in Chrome; it now allows exactly the client's redirect origin.

## 4. Open items

1. **Publishing readiness:** license, security policy, README polish and GitHub
   repository settings (tracked in the hub as a task).
2. **Claude.ai connector:** refresh tokens can fail after a cold start. The
   always-on replica (`min_replicas = 1`) and OAuth outcome logging
   (`oauth_http`, `oauth_reject`) were added to diagnose and avoid this.
3. **Per-agent identity:** the agent label (`X-Collab-Agent`) is self-declared.

## 5. Everyday use

Open an agent in the repository folder and use phrases such as:

- `Get context for project <slug> from collab` — start work.
- `Record a decision in collab: …` / `Add a verified memory in collab: …`
- `Create a task in collab: …`, `Claim task <id>`, `Complete it with evidence`
- `Send a message in collab to codex: …`, `Check my collab inbox`
- `Archive project <slug> in collab` (through `project_update`)

Sessions and handoff between clients: `docs/workflow.md`. Roadmap (phases 2–3):
`docs/roadmap.md`.

Health check: `claude mcp list` (collab: Connected),
`Get-Content $env:LOCALAPPDATA\ai-collab-hub\refresh.log -Tail 5`.

## 6. Deferred (low priority)

- a separate Entra identity per agent;
- export and backup of hub data;
- private networking (VNet and private endpoints) and semantic search
  (Azure AI Search).

## 7. Questions for the owner

- Does ChatGPT need write access, or is read-only enough? It depends on what
  the ChatGPT plan allows.
- Process: which tasks each agent takes, and whether agents should write a
  summary into the hub at the end of every session.
- Is ~$5 per month for ACR acceptable, or worth saving?
- A second user: the workspace model already supports isolation.
