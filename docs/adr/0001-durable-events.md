# ADR 0001: Durable agent events and an opt-in Windows bridge

- **Status:** proposed (feature branch `feature/event-delivery-bridge`)
- **Date:** 2026-10-11
- **Scope:** phase A (durable events in the hub) and phase B (local bridge). Phase C
  (runners) is out of scope; see `docs/roadmap.md`.

## Context

Agents leave each other messages, hand off tasks and record decisions, but nothing
tells the receiving side that something happened. The hub has no push channel: the
server is stateless (`stateless_http=True`, JSON responses, any replica serves any
request), and the clients differ:

- Claude Code and Codex run on the user's PC and can be reached by a local process.
- ChatGPT and Claude.ai / Desktop run in vendor clouds; they call the hub only during
  a conversation. Nothing can wake them.

Constraints: one Azure Table (`hub`) and one blob container, no new Azure resources,
roles or infrastructure; tools stay typed CRUD; stored content is data, never orders.

## Decision

1. **Event rows next to the change (outbox in the same partition).** Every task,
   message and decision mutation adds one event row to the **same** entity-group
   transaction as the record change: PartitionKey `<ws>~<kind>` (the source record's
   partition), RowKey `x:<ULID>`. The ULID is not inverted, so ascending RowKey order
   is chronological. (`e:` is already used for session resume events.)
2. **Subscriptions are ordinary records** (kind `subscription`, partition
   `<ws>~subscription`) written through the normal `_create` / `_save` path:
   revision, ETag, audit fields. The RowKey is the natural key
   `r:<sha256(owner_actor|name)[:40]>`.
3. **Pull, not push.** Five tools: `event_subscribe`, `event_subscriptions`,
   `event_unsubscribe`, `event_poll`, `event_ack`. Polling is a pure read; acking
   moves only the caller's own cursors with an ETag-guarded replace.
4. **Bridge.** `scripts/bridge.py` is an opt-in poller in the user's Windows session
   that shows events (console or toast) and acks them; `scripts/bridge-task.ps1`
   runs it at logon if, and only if, the user installs it. It reads and acknowledges;
   it never acts.

### Event schema (v1)

| Property | Value |
|---|---|
| `row_type` / `schema_version` | `event` / `1` |
| `event_id` | the ULID (also in the RowKey) |
| `event_type` | `message.sent`, `message.replied`, `task.created`, `task.claimed`, `task.handoff_requested`, `task.accepted`, `task.completed`, `task.blocked`, `task.released`, `decision.created` |
| `workspace`, `kind`, `project` | where it happened |
| `record_id`, `record_revision` | the record and the revision this change produced |
| `occurred_at`, `actor` | UTC time, `alias/agent` of the writer |
| `to_agent` | intended recipient label, `any`, or `""` (not addressed) |
| `payload` | small JSON summary: title/subject, status, priority, thread/reply IDs, `claimed_by`, `handoff_to/from`, `checkpoint_id`, `previous_owner`, `supersedes` |

Never in an event: message bodies, task descriptions, results, evidence, blockers,
handoff notes, checkpoint content, tokens or credentials. Addressing:
`message.*` → the message's recipient; `task.handoff_requested` → the offered label;
`task.accepted` → the previous owner's label; everything else is unaddressed.

### Subscription and delivery rules

- **Filters:** `agents` (default: the caller's label and `any`; unaddressed events
  always match; `[]` = all), `event_types` (`[]` = all; also selects which partitions
  are read), `projects` (`[]` = all). Filters run as OData filters in the table
  service, so non-matching events never consume the page budget.
- **Position:** a new subscription starts `now` unless `start_from="beginning"` or an
  ISO time is given; an existing one keeps its position (unsubscribe + subscribe to
  reset). Offline consumers catch up from their cursor.
- **Poll:** for each kind in scope read `x:` rows from
  `max(start, cursor_time − 120 s)` upwards, drop IDs already acknowledged inside that
  window, merge, sort by `event_id`, return the first `limit` (1–50) and `more`.
- **Ack:** `event_ack(name, event_ids)` locates each event in the caller's workspace,
  sets `cursors[kind]` to the highest acknowledged RowKey (never backwards) and keeps
  `[event_id, kind]` pairs whose RowKey is still inside the replay window (cap 500).
  Unknown IDs come back in `not_found`; repeats in `already_acked`. Acks do not write
  history rows (`_save(history=False)`): they are delivery bookkeeping, not content.
- **Replay window (120 s):** a ULID is minted before its transaction commits, so an
  event can become visible after a newer one was already acknowledged. Re-reading the
  last 120 s before the cursor delivers such late events once; `acked_recent`
  prevents repeats of the others.

## Consistency guarantee

- **Write side:** an event row exists **if and only if** the change it describes
  committed. Both rows are in one partition and one entity-group transaction; a
  failed or conflicting transaction (lost claim race, stale ETag) rolls back both.
  An idempotent replay returns before any transaction runs, so it adds no event.
  Changes that are not in the event list (memory edits, task field edits, unblock,
  cancel, mark-read, sessions) produce no event.
- **Read side:** delivery is **at-least-once** per subscription. Polling twice before
  acking returns the same events; an ack that fails after the poll causes
  redelivery; a full `acked_recent` (500 within 120 s) can redeliver. Consumers dedupe
  by `event_id`. Order is ascending `event_id` (writer clock order), which is close to
  but not guaranteed to be commit order.
- **Bound:** an event whose commit is delayed more than 120 s after its ULID was
  minted, or one the consumer skipped while acknowledging later events, is not
  redelivered once the cursor is more than 120 s past it (tested as a documented
  limitation).
- Multiple replicas need no coordination: polls are reads, acks are ETag-guarded and
  merge-retried (up to 4 attempts).

## Security boundary

- Workspace comes from the verified principal only; every read and write uses the
  caller's workspace partitions (tested with a second workspace).
- A subscription belongs to `owner_actor` (`alias/agent`). Only that actor may poll,
  ack or unsubscribe it; others get `forbidden`. Subscribing needs the writer role.
- The agent label is **self-declared** (`X-Collab-Agent`), exactly as for the inbox
  today: a client can subscribe with `agents=["codex"]` and see codex-addressed
  events. This is not a security boundary between agents of the same user; the
  verified boundary is the user (alias) and the workspace.
- Events are untrusted data: every returned event carries a `notice`, tool
  descriptions and server instructions repeat that an event never authorizes work.
- Logs keep one `tool_call` line per call (tool, actor, fingerprint, outcome, ms):
  no event content, filters or tokens. The bridge logs event summaries locally and
  never tokens or headers.
- The bridge cannot do more than the user's own token allows for those three tools,
  and it is written to never run commands, launch agents, accept handoffs, claim
  tasks, mark messages read or touch repositories. Its only child process is a fixed
  toast script; event text reaches it via environment variables and is XML-escaped.

## Limitations (v1)

- No push: Claude Code/Codex learn about events through the bridge (toast/console)
  or by calling `event_poll` at session start; ChatGPT and Claude.ai only when the
  user asks in a conversation.
- Retention is unbounded: events are small (≈0.5–1 KB) and never deleted. An
  `event_prune` tool or a lifecycle job can be added later.
- At-least-once with the 120 s bound above; consumers must ack contiguously.
- Per-agent identity is still self-declared.
- A poll with a scope of N kinds costs 1 point read + N range queries even when idle.

## Alternatives rejected

- **Azure Service Bus / Event Grid:** real push and DLQs, but new resources, roles,
  network endpoints and cost; and the consumers (vendor clouds, a laptop) cannot
  receive webhooks anyway. Atomicity with the table write would need an outbox
  anyway.
- **Redis (pub/sub or streams):** another always-on resource with its own auth and
  persistence story; pub/sub loses messages for offline consumers.
- **SSE / server push from the MCP server:** the server is stateless and scales to
  several replicas; clients reconnect per request; vendor-hosted clients do not keep
  a stream open between turns. Push would need sticky sessions and a fan-out bus.
- **A separate event table or partition:** not atomic with the record change (entity
  group transactions are per partition), so an event could exist for a change that
  failed, or be missing for one that committed; it would also need a new table and
  role assignment.
- **Reusing `message.read` as delivery state:** one shared flag cannot track several
  consumers, and acking would change the user's inbox.
