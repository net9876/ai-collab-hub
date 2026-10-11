# Shared-context roadmap

## Phase 1 — sessions, checkpoints, explicit handoffs (this change)

Structured checkpoints saved on request, resumed in another client by ID, explicit
task ownership transfer (`task_handoff` / `task_accept`). No automatic capture, no
file transfer, no agent invocation. See `docs/workflow.md`.

## Phase 2 — conversation threads and user-approved transcript capture

- Thread records grouping several sessions of one topic across clients.
- **Opt-in** transcript capture: the user explicitly asks to store a conversation
  (or excerpt); stored separately from checkpoints, with retention and deletion.
- Search across checkpoints/threads (Azure AI Search free tier if volume grows).
- Optional client hooks (e.g. a coding client's session-start reminder) only after
  the client's hook lifecycle is verified to fire reliably.

## Phase 3 — capabilities and delegation

1. **Agent capability registry**: what each client can do (repo access, shell, web,
   write tools on its plan), declared and reviewed by the user.
2. **Manual delegation**: the user asks one agent to prepare a task package for
   another; the user starts the other client. Still no automatic invocation.
3. **Bounded runners**: only then, narrowly scoped automated execution (allow-listed
   repos/commands, budgets, approval gates, audit), designed as a separate service.

Each phase needs the user's go-ahead; none is implied by phase 1.

## Event delivery (ADR 0001)

- **Phase A — durable events (this change):** event rows written atomically with
  task, message and decision changes; per-consumer subscriptions with
  `event_poll` / `event_ack` (at-least-once, pull only).
- **Phase B — Windows bridge (this change):** opt-in `scripts/bridge.py` and
  `scripts/bridge-task.ps1`; shows events as toasts or console lines and
  acknowledges them. Reads only; never acts.
- **Phase C — opt-in runners (future, not built):** a separate, explicitly
  installed component that may start work in response to an event only through an
  approval gate the user answers each time (or a narrow allow-list the user wrote),
  with allow-listed repositories/commands, budgets, audit and a kill switch. It
  depends on phase 3 (capability registry, bounded runners) and needs its own ADR
  and the user's go-ahead.
- Later: `event_prune` / retention, more event types (task cancelled/unblocked,
  session checkpoints) if they prove useful.
