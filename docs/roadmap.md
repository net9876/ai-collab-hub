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
