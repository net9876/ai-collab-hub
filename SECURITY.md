# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's
private reporting instead: **Security → Report a vulnerability** on this
repository. Include what you found, how to reproduce it and the impact.

This is a personal open-source project maintained on a best-effort basis. Expect
an acknowledgement within about a week.

## Scope

In scope: the MCP server (`server/`), its OAuth server, the Terraform in
`infra/` and the scripts in `scripts/`. The threat model and known limits are in
[docs/security.md](docs/security.md).

## If you deploy this yourself

- Never commit `*.tfvars`, `backend.hcl`, `.env*`, `.mcp.json`, `.codex/` or
  `.collab.local.json`; they are git-ignored on purpose.
- The deployment uses managed identity and GitHub OIDC; there should be no
  stored credentials. If you ever find one in the repository or its history,
  rotate it and report it.
- Run `scripts/validate.ps1` (includes a secret scan) before every push.
