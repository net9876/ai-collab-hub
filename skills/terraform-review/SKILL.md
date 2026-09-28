---
name: terraform-review
description: Use when asked to review Terraform code or a terraform plan (a PR, a module, or plan output) for correctness, security, cost and state safety. Not for designing new architecture (use azure-architecture).
---

# Terraform review

## Steps

1. **Establish the baseline.** Identify Terraform and provider versions
   (`versions.tf`, `.terraform.lock.hcl`) and the backend. Note the latest
   released versions from registry.terraform.io if relevant to a finding.
2. **Run the static checks** that are available, and paste their summary:
   - `terraform fmt -check -recursive`
   - `terraform init -backend=false` then `terraform validate`
   - `tflint` and `checkov -d .` or `trivy config .` if installed. Say which
     ones were not available instead of skipping silently.
3. **Read the plan, not only the code**, when a plan exists. For every
   `destroy` or `replace` (`-/+`), explain the cause and whether data is lost.
   Never run `apply` as part of a review.
4. **Check these categories**, one finding per bullet with file:line:
   - *State safety:* resources that would be recreated by a rename (missing
     `moved` blocks), `prevent_destroy` / locks on stateful resources, backend
     auth (`use_azuread_auth`), no secrets in outputs or state where avoidable.
   - *Security:* public network access, anonymous blob access, TLS minimum,
     key-based auth disabled where possible, role assignments at the narrowest
     scope, no `Owner`/`Contributor` for workloads.
   - *Correctness:* dependency cycles, `count`/`for_each` keys that churn,
     provider-version-specific arguments (check the upgrade guide), implicit
     defaults that changed between major versions.
   - *Cost:* SKUs, retention, always-on replicas, diagnostic settings volume.
   - *Operability:* tags, naming, outputs needed by scripts, idempotent reruns.
5. **Classify** each finding as blocker / should-fix / nit, and propose the
   concrete change (code snippet). Distinguish verified findings from
   suspicions you could not confirm.
