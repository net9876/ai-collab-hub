terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.10"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Partial configuration: `terraform init -backend-config=backend.hcl`.
  # backend.hcl is git-ignored; generate it from the bootstrap output `backend_hcl`.
  backend "azurerm" {}
}
