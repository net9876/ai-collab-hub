# Decisions

Architecture and process decisions, lightweight ADR style. The hub
(`decision_add`, `decision_search`) holds working decisions; significant ones
are copied here through a normal Git review so they are versioned with the
code.

Decisions are append-only. To change one, add a new decision that
`supersedes` the old ID; never edit the old text beyond marking it superseded.

## Format

```markdown
### D-<YYYYMMDD>-<short-slug>

- **Status:** proposed | accepted | superseded by D-...
- **Date:** YYYY-MM-DD (UTC)
- **Project:** <slug>
- **Decided by:** <role / agent>, confirmed by <owner or "pending">
- **Context:** why a decision was needed (facts, constraints).
- **Decision:** what was chosen, in one or two sentences.
- **Alternatives considered:** option — why rejected.
- **Consequences:** costs, risks, follow-up tasks.
- **Evidence:** links to docs, benchmarks, plans, PRs.
```

## Example (fictional)

### D-20260101-state-backend

- **Status:** accepted
- **Date:** 2026-01-01
- **Project:** contoso-landing-zone
- **Decided by:** platform engineer, confirmed by owner
- **Context:** Terraform state must be shared between laptop and CI.
- **Decision:** Azure Blob backend with Entra ID auth and a delete lock.
- **Alternatives considered:** Terraform Cloud — extra account and cost;
  local state — not shareable.
- **Consequences:** bootstrap step required once per subscription.
- **Evidence:** `infra/bootstrap/README.md` (fictional)
