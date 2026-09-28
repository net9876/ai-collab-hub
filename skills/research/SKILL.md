---
name: research
description: Use when a question depends on current external facts (product features, versions, prices, limits, API behavior) that must be checked against official sources before answering or building. Not for questions answerable from the repo itself.
---

# Research against current sources

## Steps

1. **Write the question list** as concrete, checkable items ("Does X support
   Y on plan Z?", "Latest version of package P?"). Unclear scope: ask first.
2. **Search the hub first**: `memory_search` and `decision_search` for the
   topic. A verified memory newer than the source's last change may already
   answer it; still note its date.
3. **Prefer primary sources** in this order: official docs and API references,
   official changelogs/release notes, package registries (PyPI, npm,
   registry.terraform.io), official GitHub repos. Blogs and forums only as
   leads, never as the sole evidence.
4. **Record for each item:** the answer, the exact URL, the version or page
   date seen, and whether it is `VERIFIED` (read on the page) or `UNVERIFIED`
   (inferred, snippet only, page inaccessible). Never fill gaps from memory
   without labelling them.
5. **Resolve conflicts** between sources by preferring the most specific and
   most recent official page; mention the conflict.
6. **Report** as a compact table or list. Store durable, verified results with
   `memory_add` (`verified: true`, source URLs in `sources`), scoped to the
   project, so the next agent does not repeat the work.
