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
| `task_handoff(task_id, expected_revision, to_agent?, note?, checkpoint_id?, evidence?)` | write | Owner only. Offers the task to an agent label (or `any`); `to_agent` omitted = withdraw. Owner unchanged until accepted; evidence is appended. |
| `task_accept(task_id, expected_revision)` | write | Recipient label only. Atomic ownership transfer (one winner), audit row, `previous_owners` updated, evidence kept. |

### Sessions and checkpoints (handoffs between clients)

| Tool | Access | Purpose |
|---|---|---|
| `session_start(project, title, task_ids?, idempotency_key?)` | write | One session per conversation/workstream; `source_client` = caller's (self-declared) label. |
| `session_list(project?, status?, source_client?, query?, limit, cursor?)` | read | Newest first; keywords match title + latest checkpoint goal. |
| `session_get(session_id, checkpoint_seq?, latest_checkpoint?)` | read | Card, 20 most recent checkpoint summaries, resume events; one full checkpoint on request. |
| `session_checkpoint(session_id, checkpoint, expected_revision?, idempotency_key?)` | write | Owner client only, active sessions only. Immutable checkpoint `<session_id>-c<seq>`. Without `expected_revision` concurrent writers are serialized (distinct seq); with it, stale → `conflict`. Max 60,000 bytes. |
| `session_resume(project, checkpoint_id \| session_id \| latest=true, detail=overview\|full, title?, idempotency_key?)` | write | Returns the exact selected checkpoint's context and a new continuation session for the caller; records a resume event on the source. `latest` with several active checkpointed sessions → `status: "ambiguous"` + candidates, nothing created. Never moves tasks. |
| `session_close(session_id, expected_revision, note?)` | write | Owner client only. Checkpoints stay resumable. |

`checkpoint` fields: `goal` (required), `summary`, `user_constraints` (what the
user said), `hypotheses` (agent assumptions, kept apart), `decisions[{text,
decision_id?}]`, `open_questions`, `completed[{item, evidence[]}]`,
`next_actions`, `blockers`, `code_state{repository_url, local_path, branch,
head_commit, dirty, changed_files[], tests[{command, result}], artifacts[]}`,
`task_ids`, `memory_ids`. Bounds: see the JSON schema from `tools/list`.

Resume response: `status`, `selected_session_id`, `selected_checkpoint_id`,
`continuation_session_id`, `detail`, `truncated`, `context`, `candidates`,
`idempotent_replay`, `notice` (provenance and scope). `overview` trims lists to 5
items, the summary to 1,500 characters and changed files to 20.

`project_get_context` now also returns up to 5 `active_sessions`.

Usage examples per client: `docs/workflow.md`.

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

Handoff (owner unchanged until accepted):

```
in_progress/blocked (owner A) ──task_handoff(to B)──► same status, handoff_to=B
        ▲                                                   │
        └──task_handoff(to_agent omitted) withdraws ────────┤
                                                            ▼
                                   task_accept by B ──► owner B, previous_owners += A
```

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
