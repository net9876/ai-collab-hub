output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "acr_login_server" {
  value = azurerm_container_registry.this.login_server
}

output "acr_name" {
  value = azurerm_container_registry.this.name
}

output "image_repository" {
  value = "${azurerm_container_registry.this.login_server}/${local.image_repo}"
}

output "container_app_name" {
  value = local.app_name
}

output "mcp_url" {
  description = "Remote MCP endpoint (Streamable HTTP)."
  value       = "https://${local.app_fqdn}/mcp"
}

output "health_url" {
  value = "https://${local.app_fqdn}/healthz"
}

output "storage_account" {
  value = azurerm_storage_account.data.name
}
