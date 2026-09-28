# Bootstrap stack

Creates what the main stack needs before it can run, once per subscription:

- Terraform **state** storage: `rg-<prefix>-tfstate`, a StorageV2 LRS account
  with shared keys **disabled** (Entra-only, `use_azuread_auth`), blob
  versioning, soft delete (`state_retention_days`), a `CanNotDelete` lock and
  `prevent_destroy`, private container `tfstate`, and Storage Blob Data
  Contributor for the operator. State locking uses blob leases.
- Entra **API app** `ai-collab-hub-api`: v2 tokens, scopes `Collab.Read` /
  `Collab.ReadWrite`, identifier URI `api://<client-id>`, service principal
  with *assignment required* and the operator assigned, Azure CLI
  pre-authorized (no consent prompt for `az account get-access-token`).
- Optional GitHub **deploy identity**: app + service principal + federated
  credential for `repo:<owner>/<repo>:environment:<env>`. No secret. Its Azure
  roles are granted by the main stack at resource scope.

It runs with **local state first** (it creates the backend), then migrates
into the account it created — see `docs/runbook.md` step 1. The local
`terraform.tfstate` is git-ignored; it contains resource IDs but no secrets
(no client secrets are created). Delete the local copy after migration.

Needs: Owner (or Contributor + User Access Administrator) on the subscription
and permission to create app registrations and grant app role assignments in
Entra (e.g. Application Administrator).
