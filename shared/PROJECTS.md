# Projects

One card per project. The slug is the key used everywhere (`project` field in
the hub, branch names, tags). Slug format: lowercase letters, digits and
hyphens, 2–40 characters, starting with a letter or digit.

The hub (`project_list`, `project_get_context`) holds the live card; this file
is the reviewed, versioned copy. Keep both short.

## Template

```markdown
### <slug>

- **Name:** <human name>
- **Purpose:** <one sentence: what problem it solves>
- **Status:** active | paused | archived
- **Repo / path:** <repo URL or local path, no secrets>
- **Owner:** <role, not personal data>
- **Key constraints:** <cost ceiling, compliance, deadlines>
- **Where to look first:** <docs, dashboards, entry points>
```

## Example (fictional)

### contoso-landing-zone

- **Name:** Contoso landing zone
- **Purpose:** Terraform modules for a fictional company's Azure landing zone.
- **Status:** active
- **Repo / path:** `github.com/contoso/landing-zone` (fictional)
- **Owner:** platform team
- **Key constraints:** monthly lab budget under $20; no public endpoints
- **Where to look first:** `docs/architecture.md`, `modules/`
