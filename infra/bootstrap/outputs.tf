output "state_resource_group" {
  value = azurerm_resource_group.state.name
}

output "state_storage_account" {
  value = azurerm_storage_account.state.name
}

output "state_container" {
  value = azurerm_storage_container.state.name
}

output "backend_hcl" {
  description = "Content for infra/terraform/backend.hcl (git-ignored)."
  value       = <<-EOT
    resource_group_name  = "${azurerm_resource_group.state.name}"
    storage_account_name = "${azurerm_storage_account.state.name}"
    container_name       = "${azurerm_storage_container.state.name}"
    key                  = "ai-collab-hub/main.tfstate"
    use_azuread_auth     = true
  EOT
}

output "api_client_id" {
  description = "Token audience (Entra v2 'aud') for the MCP server."
  value       = azuread_application.api.client_id
}

output "api_scope" {
  description = "Scope clients request, e.g. az account get-access-token --scope <this>."
  value       = "api://${azuread_application.api.client_id}/Collab.ReadWrite"
}

output "operator_object_id" {
  description = "Object ID of the operator (goes into collab_principals)."
  value       = data.azurerm_client_config.current.object_id
}

output "deploy_client_id" {
  description = "AZURE_CLIENT_ID for the GitHub deploy workflow (store as a GitHub secret)."
  value       = one(azuread_application.deploy[*].client_id)
}

output "deploy_principal_object_id" {
  value = one(azuread_service_principal.deploy[*].object_id)
}
