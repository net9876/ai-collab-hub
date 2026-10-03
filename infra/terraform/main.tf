data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

locals {
  name     = "${var.prefix}-${var.environment}"
  compact  = "${var.prefix}${var.environment}${random_string.suffix.result}"
  app_name = "ca-${local.name}"
  tags     = merge(var.tags, { environment = var.environment })

  table_name     = "hub"
  container_name = "content"
  image_repo     = "collab-hub"
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.name}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = local.tags
}

# ---------------------------------------------------------------------------
# Data storage: Table (state/index) + Blob (Markdown bodies). Entra-only.
# ---------------------------------------------------------------------------

resource "azurerm_storage_account" "data" {
  name                            = substr("st${local.compact}", 0, 24)
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = var.storage_replication
  access_tier                     = "Hot"
  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  # Consumption Container Apps without a VNet have no stable egress IP, so the
  # endpoint stays public; every request still needs an Entra token + RBAC.
  public_network_access = "Enabled"
  tags                  = local.tags

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = var.blob_soft_delete_days
    }

    container_delete_retention_policy {
      days = var.blob_soft_delete_days
    }
  }
}

resource "azurerm_storage_container" "content" {
  name                  = local.container_name
  storage_account_id    = azurerm_storage_account.data.id
  container_access_type = "private"
}

resource "azurerm_storage_table" "hub" {
  name               = local.table_name
  storage_account_id = azurerm_storage_account.data.id
}

resource "azurerm_storage_management_policy" "data" {
  storage_account_id = azurerm_storage_account.data.id

  rule {
    name    = "expire-old-versions"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${local.container_name}/"]
    }

    actions {
      version {
        delete_after_days_since_creation = var.noncurrent_version_days
      }
    }
  }
}

resource "azurerm_management_lock" "data" {
  count      = var.protect_data ? 1 : 0
  name       = "protect-collab-data"
  scope      = azurerm_storage_account.data.id
  lock_level = "CanNotDelete"
  notes      = "AI Collab Hub data. Set protect_data = false and apply before teardown."
}

resource "azurerm_monitor_diagnostic_setting" "blob" {
  count                      = var.enable_storage_diagnostics ? 1 : 0
  name                       = "to-law"
  target_resource_id         = "${azurerm_storage_account.data.id}/blobServices/default"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "StorageWrite"
  }

  enabled_log {
    category = "StorageDelete"
  }

  enabled_metric {
    category = "Transaction"
  }
}

resource "azurerm_monitor_diagnostic_setting" "table" {
  count                      = var.enable_storage_diagnostics ? 1 : 0
  name                       = "to-law"
  target_resource_id         = "${azurerm_storage_account.data.id}/tableServices/default"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "StorageWrite"
  }

  enabled_log {
    category = "StorageDelete"
  }

  enabled_metric {
    category = "Transaction"
  }
}

# ---------------------------------------------------------------------------
# Identity: one user-assigned identity for the app. It exists (with its role
# assignments) before the Container App, so the first revision can pull the
# image and reach storage without a chicken-and-egg problem.
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.name}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "app_blob" {
  scope                = "${azurerm_storage_account.data.id}/blobServices/default/containers/${local.container_name}"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
  depends_on           = [azurerm_storage_container.content]
}

resource "azurerm_role_assignment" "app_table" {
  scope                = "${azurerm_storage_account.data.id}/tableServices/default/tables/${local.table_name}"
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
  depends_on           = [azurerm_storage_table.hub]
}

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

resource "azurerm_container_registry" "this" {
  name                          = substr("acr${local.compact}", 0, 50)
  resource_group_name           = azurerm_resource_group.this.name
  location                      = azurerm_resource_group.this.location
  sku                           = var.acr_sku
  admin_enabled                 = false
  anonymous_pull_enabled        = false
  public_network_access_enabled = true # Basic SKU has no private endpoints
  tags                          = local.tags
}

resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# The operator pushes the first image from the laptop.
resource "azurerm_role_assignment" "operator_acr_push" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_client_config.current.object_id
}

# ---------------------------------------------------------------------------
# Container Apps (consumption)
# ---------------------------------------------------------------------------

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${local.name}"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = local.tags

  # Azure now creates environments with the serverless "Consumption" workload
  # profile by default; declare it so plans do not try to remove it. No cost
  # by itself (billed per app usage, scale to zero).
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

locals {
  app_fqdn = "${local.app_name}.${azurerm_container_app_environment.this.default_domain}"
  principals_json = jsonencode({
    for oid, p in var.collab_principals : oid => {
      alias     = p.alias
      workspace = p.workspace
      role      = p.role
    }
  })
  app_env = {
    COLLAB_STORAGE_ACCOUNT            = azurerm_storage_account.data.name
    COLLAB_MANAGED_IDENTITY_CLIENT_ID = azurerm_user_assigned_identity.app.client_id
    COLLAB_TABLE_NAME                 = local.table_name
    COLLAB_BLOB_CONTAINER             = local.container_name
    COLLAB_TENANT_ID                  = data.azurerm_client_config.current.tenant_id
    COLLAB_OIDC_AUDIENCE              = var.api_client_id
    COLLAB_PRINCIPALS                 = local.principals_json
    COLLAB_PUBLIC_URL                 = "https://${local.app_fqdn}"
    COLLAB_LOG_LEVEL                  = "INFO"
    COLLAB_OAUTH_ENABLED              = tostring(var.enable_oauth)
    COLLAB_OAUTH_LOGIN_CLIENT_ID      = var.enable_oauth ? azuread_application.login[0].client_id : ""
  }
}

# ---------------------------------------------------------------------------
# OAuth for remote connectors (ChatGPT, Claude.ai / Desktop). The hub is the
# OAuth authorization server (DCR + PKCE) and delegates user sign-in to this
# Entra app. The app authenticates to Entra with the Container App's managed
# identity (federated identity credential): no client secret, no Key Vault.
# ---------------------------------------------------------------------------

data "azuread_client_config" "current" {}

data "azuread_service_principal" "graph" {
  count     = var.enable_oauth ? 1 : 0
  client_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
}

locals {
  graph_openid_scope_id = "37f7f235-527c-4136-accd-4a02d197296e" # Graph delegated 'openid'
}

resource "azuread_application" "login" {
  count            = var.enable_oauth ? 1 : 0
  display_name     = "ai-collab-hub-login"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azuread_client_config.current.object_id]

  web {
    redirect_uris = ["https://${local.app_fqdn}/oauth/callback"]
  }

  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000"
    resource_access {
      id   = local.graph_openid_scope_id
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "login" {
  count                        = var.enable_oauth ? 1 : 0
  client_id                    = azuread_application.login[0].client_id
  app_role_assignment_required = true # only assigned users can sign in
  owners                       = [data.azuread_client_config.current.object_id]
}

resource "azuread_app_role_assignment" "login_users" {
  for_each            = var.enable_oauth ? var.collab_principals : {}
  app_role_id         = "00000000-0000-0000-0000-000000000000" # default access
  principal_object_id = each.key
  resource_object_id  = azuread_service_principal.login[0].object_id
}

# Pre-consent 'openid' for the allowed users so sign-in shows no consent prompt.
resource "azuread_service_principal_delegated_permission_grant" "login_openid" {
  for_each                             = var.enable_oauth ? var.collab_principals : {}
  service_principal_object_id          = azuread_service_principal.login[0].object_id
  resource_service_principal_object_id = data.azuread_service_principal.graph[0].object_id
  claim_values                         = ["openid"]
  user_object_id                       = each.key
}

resource "azuread_application_federated_identity_credential" "login_mi" {
  count          = var.enable_oauth ? 1 : 0
  application_id = azuread_application.login[0].id
  display_name   = "container-app-managed-identity"
  description    = "The hub's user-assigned managed identity acts as this app's credential."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
  subject        = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_container_app" "this" {
  count                        = var.deploy_app ? 1 : 0
  name                         = local.app_name
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  ingress {
    external_enabled           = true
    target_port                = 8000
    transport                  = "auto"
    allow_insecure_connections = false

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }

    container {
      name   = "collab-hub"
      image  = var.container_image
      cpu    = var.app_cpu
      memory = var.app_memory

      dynamic "env" {
        for_each = local.app_env
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        transport               = "HTTP"
        path                    = "/healthz"
        port                    = 8000
        interval_seconds        = 2
        failure_count_threshold = 30
      }

      liveness_probe {
        transport        = "HTTP"
        path             = "/healthz"
        port             = 8000
        interval_seconds = 30
      }

      readiness_probe {
        transport        = "HTTP"
        path             = "/healthz"
        port             = 8000
        interval_seconds = 10
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.container_image != ""
      error_message = "deploy_app = true needs container_image (push one first; see docs/runbook.md)."
    }
    # Image tags are rolled forward by the deploy workflow; Terraform owns the rest.
    ignore_changes = [template[0].container[0].image]
  }

  depends_on = [
    azurerm_role_assignment.app_acr_pull,
    azurerm_role_assignment.app_blob,
    azurerm_role_assignment.app_table,
  ]
}

# ---------------------------------------------------------------------------
# GitHub OIDC deploy identity: push images + roll the app's revision. Nothing else.
# ---------------------------------------------------------------------------

resource "azurerm_role_assignment" "deploy_acr_push" {
  count                = var.deploy_principal_object_id == null ? 0 : 1
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = var.deploy_principal_object_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deploy_app" {
  count                = var.deploy_principal_object_id != null && var.deploy_app ? 1 : 0
  scope                = azurerm_container_app.this[0].id
  role_definition_name = "Container Apps Contributor"
  principal_id         = var.deploy_principal_object_id
  principal_type       = "ServicePrincipal"
}

# Updating an app that uses a user-assigned identity requires identity/assign.
resource "azurerm_role_assignment" "deploy_identity" {
  count                = var.deploy_principal_object_id == null ? 0 : 1
  scope                = azurerm_user_assigned_identity.app.id
  role_definition_name = "Managed Identity Operator"
  principal_id         = var.deploy_principal_object_id
  principal_type       = "ServicePrincipal"
}
