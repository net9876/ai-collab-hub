# Working preferences (example template)

Copy to `shared/PREFERENCES.md` (git-ignored) and replace the `TODO(owner)`
fields. Agents read it when present; absence is fine.

## Communication

- Report language: `TODO(owner)` — e.g. "short reports in English"
- Detail level: `TODO(owner)` — e.g. "explain the why before big changes"

## Engineering defaults

- IaC tool: `TODO(owner)` — e.g. "Terraform, azurerm provider"
- Cost posture: `TODO(owner)` — e.g. "lab: smallest SKUs, scale to zero, tear down after use"
- Confirmation required before: `TODO(owner)` — e.g. "terraform apply, git push, anything billable"

## Example (fictional)

- Reports: short, bullet points, English
- IaC: Terraform with remote state; no ClickOps
- Always show `terraform plan` and a cost estimate before `apply`
