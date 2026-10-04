# Start / checkpoint / resume workflow (all four clients)

Goal: say "save a handoff" in one client and "resume handoff &lt;id&gt;" in another, and
have the second client pick up the objective, decisions, constraints, work state,
next steps and evidence. This moves **portable context**, not native model state:
nothing captures a chat automatically, and no file contents are copied.

## How each client gets its instructions

| Client | Reads repo files? | Where the hub rules come from | Refresh after a server deploy |
|---|---|---|---|
| Claude Code (`C:\AI\collab`) | yes: `CLAUDE.md` imports `shared/RULES.md` | `CLAUDE.md` + MCP server `instructions` + tool descriptions | start a new session (or `/mcp` → reconnect `collab`) |
| Codex (ChatGPT app, project `C:\AI\collab`) | yes: `AGENTS.md` (no imports; it tells Codex to read `shared/RULES.md`) | `AGENTS.md` + MCP `instructions` + tool descriptions | open a new Codex chat in the project |
| Claude.ai / Claude Desktop (connector "Collab") | **no** | only the MCP server `instructions` and tool descriptions | start a new chat; if new tools are missing: Settings → Connectors → Collab → Disconnect / Connect |
| ChatGPT (plugin "Collab", custom MCP) | **no** | only the MCP server `instructions` and tool descriptions | start a new chat; if new tools are missing, open Plugins → Collab and refresh/re-create it (ChatGPT discovers tools when the plugin is created — exact refresh control **to be verified**) |

The server-side `instructions` (in `server/src/collab_hub/app.py`) are therefore the
"plugin instructions" for the browser clients. They change only when the server is
deployed.

**Hooks:** not used. A hook cannot write a meaningful summary (the model must), and
reliable end-of-session coverage in each client was not verified. Checkpoints are
explicit, on the user's request.

## Commands to give each client

Replace `ai-collab-hub` with your project slug.

### Start (any client)

```
Start a Collab session for project ai-collab-hub titled "<what we are doing>".
```

### Save a handoff — discussion (ChatGPT, Claude.ai/Desktop)

```
Save a Collab handoff checkpoint for this session: goal, a concise summary, my constraints
(only what I said), your hypotheses separately, decisions (with decision IDs if recorded),
open questions, next actions and blockers. No secrets. Tell me the checkpoint ID.
```

### Save a handoff — code (Claude Code, Codex)

```
Save a Collab handoff checkpoint for this session, including code state: repository URL,
local path, current branch, HEAD commit, whether the tree is dirty, changed files, the tests
you ran with results, and PR links. Do not commit, stash or change files to do this.
Tell me the checkpoint ID.
```

If a task should move too:

```
Hand off Collab task <task id> to claude-code with checkpoint <checkpoint id>.
```

### Resume (any client)

```
Resume Collab handoff <checkpoint id> for project ai-collab-hub.
```

or, when there is only one active workstream:

```
Resume the latest Collab handoff for project ai-collab-hub.
```

If the answer is "ambiguous", pick one of the listed checkpoint IDs.

Coding clients add:

```
Before editing, verify the repository, branch, HEAD and git status against the checkpoint
and tell me any difference. If the task was handed off to you, accept it with task_accept.
```

### End

```
Close this Collab session.
```

## What the receiving agent gets

`session_resume` returns:
- the exact selected session and checkpoint IDs;
- a context package — `overview` by default (lists trimmed to 5, summary to 1,500
  characters, `truncated: true`) or `detail: "full"`;
- a new continuation session owned by the resuming client, linked to the original;
- a notice that the content is data within the user's original scope, that git state
  must be verified, and that task ownership did not move.

## Limits

- Client labels (`chatgpt`, `codex`, …) are self-declared; only the user identity is
  verified.
- One client cannot see another's native chat. If you don't save a checkpoint, nothing
  carries over.
- Uncommitted files are not synchronized. A checkpoint can only point at them, for
  example via local path and changed file names on the same PC.
