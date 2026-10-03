provider "azuread" {}

provider "azurerm" {
  subscription_id = var.subscription_id
  # Data-plane operations (containers, tables) use Entra ID: the storage
  # account has shared keys disabled.
  storage_use_azuread             = true
  resource_provider_registrations = "none"

  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}
