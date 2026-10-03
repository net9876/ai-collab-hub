# MCP API v1

Endpoint: `https://<app-fqdn>/mcp` (Streamable HTTP, stateless, JSON
responses). Health: `GET /healthz` (no auth, returns only status/version).
Protected resource metadata: `GET /.well-known/oauth-protected-resource/mcp`.

Exact JSON schemas are served by `tools/list`; this page is the overview.

## Conventions

- **Identity.** The author of every write (`created_by`, `updated_by`,
  revision `actor`) is `<alias>/<agent>`: `alias` comes from the verified
  token (Entra `oid` mapped in `COLLAB_PRINCIPALS`), `agent` from the
  `X-Collab-Agent` header (`claude-code`, `claude-desktop`, `codex`,
  `chatgpt`, `other`). The agent label is **self-declared**; the alias is
  verified. No tool accepts an author argument.
- **IDs.** 26-char ULIDs (projects use their slug). Timestamps are UTC ISO-8601.
- **Revisions.** Every record has `revision` (starts at 1). Updates take
  `expected_revision` and fail with `conflict` if stale. Nothing is silently
  overwritten; memory history is readable.
- **Idempotency.** Create tools accept `idempotency_key` (8–64 chars
  `[A-Za-z0-9_-]`). Same key + same arguments → the original record with
  `idempotent_replay: true`; same key + different arguments → `conflict`.
- **Pagination.** `limit` 1–50 (default 20) and opaque `cursor` →
  `{items, next_cursor, scanned}`. Lists are newest first (projects by slug).
- **Errors.** Tool results with `isError: true` and text
  `<code>: <message>`; codes: `validation`, `not_found`, `conflict`,
  `forbidden`, `unavailable` (storage problem, retry later). Schema
  violations are rejected before the tool runs. Auth failures are HTTP 401.
- **Limits.** Request body 256 KiB; memory body 100k chars; message body
  8k; task description 8k; decision fields 4k each; 12 tags; 20 sources /
  evidence entries.

## Tools

| Tool | Access | Purpose |
|---|---|---|
| `project_create(slug, name, purpose, idempotency_key?)` | write | Register a project card. Only on user request. |
| `project_update(slug, expected_revision, name?, purpose?, status?)` | write | Edit a card or set status `active`/`paused`/`archived`; history kept. |
| `project_list(status?, limit, cursor?)` | read | Find project slugs. |
| `project_get_context(project)` | read | Card + ≤10 decisions (not superseded) + ≤20 open/in-progress/blocked tasks + ≤10 active memories + your unread count. |
| `memory_add(project, title, body, tags?, verified?, sources?, idempotency_key?)` | write | New memory (body in Blob). |
| `memory_get(memory_id, revision?, include_history?)` | read | Full body, current or past revision; history list. |
| `memory_search(query?, project?, tags?, status=active, verified_only?, limit, cursor?)` | read | Keyword + metadata; returns snippets. |
| `memory_update(memory_id, expected_revision, title?, body?, tags?, verified?, sources?, status?, change_note?)` | write | New revision; `status="archived"` retires. |
| `decision_add(project, title, context, decision, alternatives?, consequences?, evidence?, status=proposed, supersedes?, idempotency_key?)` | write | Record a decision; `supersedes` marks the old one atomically. |
| `decision_search(query?, project?, status?, limit, cursor?)` | read | Find decisions. |
| `task_create(project, title, description?, priority?, labels?, idempotency_key?)` | write | Open task. Never triggers execution. |
| `task_get(task_id)` | read | One task. |
| `task_list(project?, status?, claimed_by_me?, query?, limit, cursor?)` | read | Filtered list. |
| `task_claim(task_id)` | write | Atomic claim of an `open` task → `in_progress`. One winner; others `conflict`. |
| `task_update(task_id, expected_revision, title?, description?, priority?, labels?, status?, release?, note?)` | write | Edit, unblock (`status=open`), cancel, or release a claim. |
| `task_complete(task_id, expected_revision, result, evidence[≥1])` | write | Claimant only. |
| `task_block(task_id, expected_revision, blocker)` | write | Claimant only. |
| `message_send(to_agent, subject, body, project?, idempotency_key?)` | write | Note to an agent or `any`. |
| `message_inbox(include_read?, project?, limit, cursor?)` | read | Messages to your agent label or `any`. |
| `message_reply(message_id, body, idempotency_key?)` | write | Reply in thread to the sender's agent. |
| `message_mark_read(message_ids[1..50])` | write | Idempotent; only for messages addressed to you. |

`project_create` and `project_update` go beyond the originally requested list:
without them no project could exist or be retired, and every other write
validates the project slug.

## OAuth endpoints (connectors)

`/.well-known/oauth-authorization-server`, `/register` (DCR), `/authorize`,
`/token`, `/revoke`, plus `/oauth/callback` (Entra return) and `/oauth/approve`
(consent form). See `docs/connect-chatgpt.md`.

## Task state machine

```
open ──task_claim──► in_progress ──task_complete──► done
  ▲                    │   ▲
  │ task_update        │   │ task_update(status=open)
  │ (release)          ▼   │
  └──────────────── blocked (task_block)
any non-final ──task_update(status=cancelled)──► cancelled
```

`done` and `cancelled` are final.

## Safety semantics in tool descriptions

Tool descriptions tell the model that returned content is data, that messages
and tasks never authorize actions, to ask the user before archiving,
superseding accepted decisions, cancelling or releasing others' tasks, and to
never store secrets or personal data without explicit request. MCP tool
annotations mark read-only and destructive tools so clients can require
confirmation.

## Example (Claude Code, after connecting)

```
> project_get_context project=ai-collab-hub
> task_create project=ai-collab-hub title="Wire ChatGPT via OAuth proxy"
> task_claim task_id=01K...   # conflict if Codex already took it
> task_complete task_id=01K... expected_revision=2 result="..." evidence=["pytest: 39 passed"]
```
