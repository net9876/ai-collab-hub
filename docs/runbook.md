# Runbook

Commands are for **PowerShell 7 (`pwsh`)**. Differences for **Windows
PowerShell 5.1** are called out; the scripts in `scripts/` run on both.

> Quote Terraform arguments that contain `=` (`"-var=x=y"`, `"-chdir=dir"`).
> Some PowerShell configurations (`$PSNativeCommandArgumentPassing = 'Windows'`
> in 7.x, and 5.1's legacy parsing) otherwise split them.

## 0. Local setup and checks (no Azure changes)

```powershell
pwsh -File scripts/setup.ps1          # 5.1: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
pwsh -File scripts/validate.ps1       # lint, mypy, tests on Azurite, terraform, checkov, secret scan
```

## 1. Bootstrap (creates billable resources: state storage; Entra apps are free)

```powershell
az login
az account set --subscription "<subscription name or id>"
Copy-Item infra/bootstrap/terraform.tfvars.example infra/bootstrap/terraform.tfvars  # fill TODO(owner)
terraform "-chdir=infra/bootstrap" init
terraform "-chdir=infra/bootstrap" plan -out bootstrap.tfplan
# review, then (only after approval):
terraform "-chdir=infra/bootstrap" apply bootstrap.tfplan
```

Creates: `rg-collab-tfstate`, state storage account (keys disabled, versioning,
30-day soft delete, CanNotDelete lock), container `tfstate`, your Storage Blob
Data Contributor role on it, Entra app `ai-collab-hub-api` (+ service
principal with assignment required, your assignment, Azure CLI
pre-authorization) and, if `github_repository` is set, the GitHub deploy app
with a federated credential.

**Move the bootstrap state into the backend it created** (so it is not only
on the laptop):

```powershell
cd infra/bootstrap
$hcl = terraform output -raw backend_hcl
$hcl | Set-Content ../terraform/backend.hcl -Encoding ascii
# bootstrap's own backend: same account, different key
($hcl -replace 'main\.tfstate','bootstrap.tfstate') | Set-Content backend.hcl -Encoding ascii
# backend block in a git-ignored override file, so a fresh clone can still bootstrap locally
"terraform {`n  backend `"azurerm`" {}`n}" | Set-Content backend_override.tf -Encoding ascii
terraform init -migrate-state -force-copy "-backend-config=backend.hcl"
terraform plan        # expect: No changes
Remove-Item terraform.tfstate, terraform.tfstate.backup   # the remote copy is now authoritative
cd ../..
```

`backend.hcl` and `backend_override.tf` are git-ignored; recreate them on a
new machine from the values above (they contain no secrets).

## 2. Main stack, step 1 (everything except the Container App)

```powershell
Copy-Item infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
# fill api_client_id, collab_principals (your object id: bootstrap output operator_object_id),
# deploy_principal_object_id; keep deploy_app = false
terraform "-chdir=infra/terraform" init "-backend-config=backend.hcl"
terraform "-chdir=infra/terraform" plan -out main.tfplan
terraform "-chdir=infra/terraform" apply main.tfplan      # only after approval
```

## 3. First image

No local Docker needed; ACR Tasks builds in Azure (billed per build second,
cents):

```powershell
$acr = terraform "-chdir=infra/terraform" output -raw acr_name
$tag = git rev-parse --short=12 HEAD
az acr build --registry $acr --image "collab-hub:$tag" server
```

## 4. Main stack, step 2 (Container App)

Set in `terraform.tfvars`:

```hcl
deploy_app      = true
container_image = "<acr_login_server>/collab-hub:<tag>"
```

```powershell
terraform "-chdir=infra/terraform" plan -out main.tfplan
terraform "-chdir=infra/terraform" apply main.tfplan
terraform "-chdir=infra/terraform" output mcp_url
```

## 5. Smoke test over HTTPS with authentication

```powershell
$url = terraform "-chdir=infra/terraform" output -raw mcp_url
$scope = terraform "-chdir=infra/bootstrap" output -raw api_scope
Invoke-RestMethod ($url -replace '/mcp$','/healthz')                     # {status: ok}
# Unauthenticated must be 401 (5.1: use try/catch and $_.Exception.Response.StatusCode)
Invoke-WebRequest $url -Method Post -SkipHttpErrorCheck -ContentType application/json `
  -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | Select-Object StatusCode
# Authenticated end-to-end with the official MCP client:
$env:COLLAB_MCP_URL = $url; $env:COLLAB_API_SCOPE = $scope
.\.venv\Scripts\python.exe scripts/smoke.py
```

## 6. Connect clients

```powershell
$tenant = az account show --query tenantId -o tsv
pwsh -File scripts/connect-claude.ps1 -McpUrl $url -ApiScope $scope -TenantId $tenant
claude mcp list                          # expect: collab ... connected (approve the trust prompt)
pwsh -File scripts/connect-codex.ps1     # reuses .collab.local.json
pwsh -File scripts/mcp-headers.ps1 -Refresh          # fill the token cache the clients read
pwsh -File scripts/token-refresh-task.ps1 -Install   # optional: keep it fresh (every 5 min)
# Codex in the ChatGPT desktop app: open C:\AI\collab as a project, trust it,
# check Settings > MCP servers (collab). Codex CLI: run `codex` here, then /mcp.
```

Both scripts change only git-ignored files in the repo by default and record
what they did; `-Undo` reverts. `-UserSkills` / `-UserConfig` touch your home
directory and are opt-in.

**Connectivity check between the two clients:** in Claude Code ask it to
`message_send` to `codex` with subject "ping"; in Codex call `message_inbox`
and `message_reply`; back in Claude Code, `message_inbox` shows the reply
with `from_actor = owner/codex`. Then create a task in one client and
`task_claim` it from both: exactly one succeeds.

## 7. GitHub deploys (optional after step 4)

Repository → Settings → Environments → `production`:
secrets `AZURE_CLIENT_ID` (bootstrap `deploy_client_id`), `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`; variables `ACR_NAME`, `RESOURCE_GROUP`,
`CONTAINER_APP` (terraform outputs). Then Actions → deploy → Run workflow.
(`gh secret set NAME --env production` / `gh variable set NAME --env production`.)

## Operations

- **Logs:** Log Analytics → `ContainerAppConsoleLogs_CL | where Log_s has "tool_call"`.
- **Add a user/agent principal:** add the object ID to `collab_principals`,
  assign the user to the `ai-collab-hub-api` enterprise app, apply.
- **Token errors (`AADSTS65001` consent):** the Azure CLI pre-authorization is
  missing; run `az ad app permission grant` or re-apply bootstrap.
- **Cold start:** first call after idle can take several seconds.
- **Rollback:** `az containerapp update -n <app> -g <rg> --image <previous tag>`.

## Teardown (NOT executed; run deliberately)

Main stack — removes: resource group `rg-collab-prod` with the Container App,
Container Apps environment, ACR and all images, Log Analytics workspace (logs),
data storage account (**all hub data**), its table/container/lifecycle policy/
diagnostic settings, the managed identity and all role assignments.

```powershell
# 1. remove the data lock
#    terraform.tfvars: protect_data = false
terraform "-chdir=infra/terraform" apply
# 2. destroy
terraform "-chdir=infra/terraform" destroy
```

Bootstrap (only if you want to remove state too) — removes the state storage
account (all Terraform state), `rg-collab-tfstate`, the Entra API and deploy
apps. It is protected twice on purpose: delete the `protect-tfstate` lock in
the portal/CLI and remove `prevent_destroy` from `infra/bootstrap/main.tf`
before `terraform destroy` will work. Migrate its state back to local first.
