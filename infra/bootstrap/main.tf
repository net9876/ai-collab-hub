data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

# ---------------------------------------------------------------------------
# Terraform remote state: dedicated RG + storage account, Entra-only access,
# versioning + soft delete, and a CanNotDelete lock.
# ---------------------------------------------------------------------------

resource "azurerm_resource_group" "state" {
  name     = "rg-${var.prefix}-tfstate"
  location = var.location
  tags     = var.tags
}

resource "azurerm_storage_account" "state" {
  name                            = "st${var.prefix}tf${random_string.suffix.result}"
  resource_group_name             = azurerm_resource_group.state.name
  location                        = azurerm_resource_group.state.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  access_tier                     = "Hot"
  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = var.tags

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = var.state_retention_days
    }

    container_delete_retention_policy {
      days = var.state_retention_days
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_container" "state" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
}

resource "azurerm_management_lock" "state" {
  name       = "protect-tfstate"
  scope      = azurerm_storage_account.state.id
  lock_level = "CanNotDelete"
  notes      = "Terraform state for ai-collab-hub. Remove deliberately before any teardown."
}

# The operator running Terraform needs data-plane access (use_azuread_auth).
resource "azurerm_role_assignment" "state_operator" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

# ---------------------------------------------------------------------------
# Entra ID: the MCP API app registration (token audience + scopes).
# The server is only a resource server; Entra issues the tokens.
# ---------------------------------------------------------------------------

resource "random_uuid" "scope_read" {}
resource "random_uuid" "scope_write" {}

locals {
  azure_cli_client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
}

resource "azuread_application" "api" {
  display_name     = var.api_app_display_name
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]

  api {
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = random_uuid.scope_read.result
      value                      = "Collab.Read"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Read AI Collab Hub"
      admin_consent_description  = "Read memories, decisions, tasks and messages in AI Collab Hub."
      user_consent_display_name  = "Read AI Collab Hub"
      user_consent_description   = "Read your AI Collab Hub records."
    }

    oauth2_permission_scope {
      id                         = random_uuid.scope_write.result
      value                      = "Collab.ReadWrite"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Read and write AI Collab Hub"
      admin_consent_description  = "Create and update records in AI Collab Hub."
      user_consent_display_name  = "Read and write AI Collab Hub"
      user_consent_description   = "Create and update your AI Collab Hub records."
    }
  }

  # The URI is managed by azuread_application_identifier_uri below (it needs the
  # client ID, which exists only after creation). Without this, every plan would
  # try to remove it and break token issuance.
  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application_identifier_uri" "api" {
  application_id = azuread_application.api.id
  identifier_uri = "api://${azuread_application.api.client_id}"
}

resource "azuread_application_pre_authorized" "azure_cli" {
  count                = var.preauthorize_azure_cli ? 1 : 0
  application_id       = azuread_application.api.id
  authorized_client_id = local.azure_cli_client_id
  permission_ids       = [random_uuid.scope_read.result, random_uuid.scope_write.result]
}

# Only explicitly assigned users can obtain tokens for this API.
resource "azuread_service_principal" "api" {
  client_id                    = azuread_application.api.client_id
  app_role_assignment_required = true
  owners                       = [data.azurerm_client_config.current.object_id]
}

resource "azuread_app_role_assignment" "operator" {
  app_role_id         = "00000000-0000-0000-0000-000000000000" # default access
  principal_object_id = data.azurerm_client_config.current.object_id
  resource_object_id  = azuread_service_principal.api.object_id
}

# ---------------------------------------------------------------------------
# GitHub Actions deploy identity: OIDC federation, no client secret.
# Its Azure role assignments are created by infra/terraform at narrow scopes.
# ---------------------------------------------------------------------------

resource "azuread_application" "deploy" {
  count        = var.github_repository == "" ? 0 : 1
  display_name = "ai-collab-hub-github-deploy"
  owners       = [data.azurerm_client_config.current.object_id]
}

resource "azuread_service_principal" "deploy" {
  count     = var.github_repository == "" ? 0 : 1
  client_id = azuread_application.deploy[0].client_id
  owners    = [data.azurerm_client_config.current.object_id]
}

resource "azuread_application_federated_identity_credential" "deploy" {
  count          = var.github_repository == "" ? 0 : 1
  application_id = azuread_application.deploy[0].id
  display_name   = "github-${var.github_environment}"
  description    = "GitHub Actions deploy workflow, environment ${var.github_environment}"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = coalesce(var.github_oidc_subject, "repo:${var.github_repository}:environment:${var.github_environment}")
}
