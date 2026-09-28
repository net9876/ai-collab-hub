variable "subscription_id" {
  description = "Target subscription ID. Supply via terraform.tfvars (git-ignored) or TF_VAR_subscription_id."
  type        = string
}

variable "tenant_id" {
  description = "Entra tenant ID of the subscription."
  type        = string
}

variable "location" {
  description = "Azure region for the state storage account."
  type        = string
  default     = "eastus"
}

variable "prefix" {
  description = "Short lowercase name prefix."
  type        = string
  default     = "collab"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,9}$", var.prefix))
    error_message = "prefix: 3-10 lowercase letters/digits, starting with a letter."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default = {
    project    = "ai-collab-hub"
    managed-by = "terraform"
    purpose    = "tfstate"
  }
}

variable "state_retention_days" {
  description = "Soft-delete retention for state blobs and containers (days)."
  type        = number
  default     = 30

  validation {
    condition     = var.state_retention_days >= 7 && var.state_retention_days <= 365
    error_message = "state_retention_days must be 7-365."
  }
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub OIDC. Empty string disables the deploy identity."
  type        = string
  default     = ""
}

variable "github_environment" {
  description = "GitHub environment the deploy workflow runs in (OIDC subject)."
  type        = string
  default     = "production"
}

variable "api_app_display_name" {
  description = "Display name of the Entra app registration that represents the MCP API."
  type        = string
  default     = "ai-collab-hub-api"
}

variable "preauthorize_azure_cli" {
  description = "Pre-authorize the Azure CLI public client for the API scopes, so `az account get-access-token` needs no consent prompt."
  type        = bool
  default     = true
}
