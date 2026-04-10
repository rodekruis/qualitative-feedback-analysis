terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.67"
    }
  }

  backend "azurerm" {
    # Partial configuration: resource_group_name and storage_account_name
    # are supplied at `terraform init` time via -backend-config flags.
    # Terraform forbids variable interpolation inside the backend block
    # ("Variables may not be used here"), so they cannot be read from
    # var.resource_group_name / var.tf_state_storage_account directly.
    # See infra/BOOTSTRAP.md for the init invocation.
    container_name = "tfstate"
    key            = "terraform.tfstate"
  }
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  features {}
}
