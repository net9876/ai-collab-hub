# Contributing

Thanks for your interest. This is a small personal project; issues and pull
requests are welcome.

1. Fork and create a branch (`feat/...`, `fix/...`, `docs/...`).
2. Set up and check locally, no Azure access needed:
   ```powershell
   pwsh -File scripts/setup.ps1
   pwsh -File scripts/validate.ps1   # ruff, mypy, pytest on Azurite, terraform, checkov, secret scan
   ```
3. Keep changes focused, add or update tests for behavior changes, and update
   the relevant file in `docs/`.
4. Open a pull request. CI (lint, types, tests, Terraform checks, gitleaks) must
   pass.

Rules:

- Never commit secrets, tenant/subscription IDs, personal paths or real
  hostnames. Use placeholders and `*.example` files.
- Hub tools stay typed CRUD only; nothing may execute commands on a client.
- Never run `terraform apply` against a shared environment without reviewing the plan.

By contributing you agree that your contribution is licensed under the MIT
License ([LICENSE](LICENSE)).
