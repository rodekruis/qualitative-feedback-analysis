locals {
  env                   = terraform.workspace
  app_name              = "qfa-${local.env}-backend"
  plan_name             = "qfa-${local.env}-plan" # Azure Web Service plan name
  acr_name              = "qfacontainerreg"       # does not support dashes
  vnet_name             = "qfa-${local.env}-vnet"       # does not support dashes
  keyvault_name         = "qfa-${local.env}-keyvault"
  managed_identity_name = "qfa-${local.env}-github"
  github_environment    = local.env # == "prd" ? "prd" : "dev"
}


# =============================================================================
# Resource Group (read-only — managed outside Terraform)
# =============================================================================

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

# =============================================================================
# Container Registry
# =============================================================================

data "azurerm_container_registry" "acr" {
  name                = local.acr_name
  resource_group_name = data.azurerm_resource_group.main.name
}


# =============================================================================
# Shared Networking
# =============================================================================

resource "azurerm_virtual_network" "qfa_vnet" {
  name = local.vnet_name
  location = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  address_space = ["10.0.0.0/16"]
}

# =============================================================================
# Key Vault
# =============================================================================

resource "azurerm_key_vault" "main" {
  name                       = local.keyvault_name
  resource_group_name        = data.azurerm_resource_group.main.name
  location                   = data.azurerm_resource_group.main.location
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 90
  purge_protection_enabled   = false
}

# Key Vault secret names are declared here, but VALUES are managed out-of-band
# (via az keyvault secret set / the update_auth_api_keys.py script).
# This avoids storing secrets in Terraform state.

# =============================================================================
# App Service
# =============================================================================

resource "azurerm_service_plan" "main" {
  name                = local.plan_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = "B1"
}

resource "azurerm_subnet" "qfa_backend_snet" {
  resource_group_name = data.azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.qfa_vnet.name
  name = "qfa-${local.env}-backend-snet"
  address_prefixes = ["10.0.1.0/24"]
  delegation {
    name = "app-service-delegation"
    service_delegation {
      name = "Microsoft.Web/serverFarms"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_linux_web_app" "backend" {
  name                                           = local.app_name
  resource_group_name                            = data.azurerm_resource_group.main.name
  location                                       = data.azurerm_resource_group.main.location
  service_plan_id                                = azurerm_service_plan.main.id
  https_only                                     = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  virtual_network_subnet_id = azurerm_subnet.qfa_backend_snet.id

  identity {
    type = "SystemAssigned"
  }

  site_config {
    always_on                         = true
    health_check_path                 = "/v1/health"
    health_check_eviction_time_in_min = 10
    http2_enabled                     = true
    ftps_state                        = "Disabled"
    minimum_tls_version               = "1.2"
    scm_minimum_tls_version           = "1.2"

    container_registry_use_managed_identity = true
  }

  app_settings = {
    LLM_PROVIDER    = var.llm_provider
    LLM_MODEL       = var.llm_model
    LLM_API_VERSION = var.llm_api_version

    # Key Vault references — the App Service resolves these at runtime
    LLM_AZURE_ENDPOINT = "@Microsoft.KeyVault(SecretUri=https://${local.keyvault_name}.vault.azure.net/secrets/llm-azure-endpoint)"
    LLM_API_KEY        = "@Microsoft.KeyVault(SecretUri=https://${local.keyvault_name}.vault.azure.net/secrets/llm-api-key)"
    AUTH_API_KEYS      = "@Microsoft.KeyVault(SecretUri=https://${local.keyvault_name}.vault.azure.net/secrets/auth-api-keys)"

    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
    WEBSITES_PORT                       = "8000"
  }

  logs {
    http_logs {
      file_system {
        retention_in_days = 3
        retention_in_mb   = 100
      }
    }
  }

  lifecycle {
    # The container image tag is updated by the CI/CD pipeline, not Terraform
    ignore_changes = [site_config[0].application_stack]
  }
}

# App Service identity: read secrets from Key Vault
resource "azurerm_role_assignment" "app_keyvault_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_linux_web_app.backend.identity[0].principal_id
}


# Grant the App Service pull access to ACR
resource "azurerm_role_assignment" "app_acr_repository_reader" {
  scope                = data.azurerm_container_registry.acr.id
  role_definition_name = "Container Registry Repository Reader"
  principal_id         = azurerm_linux_web_app.backend.identity[0].principal_id
}



# =============================================================================
# GitHub CI/CD
# =============================================================================

# Grant GitHub Actions write access to ACR (for CI/CD image builds)
resource "azurerm_role_assignment" "github_acr_repository_writer" {
  scope                = data.azurerm_container_registry.acr.id
  role_definition_name = "Container Registry Repository Writer"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

resource "azurerm_user_assigned_identity" "github" {
  name                = local.managed_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

resource "azurerm_federated_identity_credential" "github_environment" {
  name                      = "gh-qualitative-feedback-analysis-${local.env}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_repo}:environment:${local.github_environment}"
}

# GitHub Actions identity gets Contributor on the resource group
resource "azurerm_role_assignment" "github_contributor" {
  scope                = data.azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

# =============================================================================
# Database
# =============================================================================
# resource "azurerm_subnet" "qfa_db_snet" {
#   resource_group_name = data.azurerm_resource_group.main.name
#   virtual_network_name = azurerm_virtual_network.qfa_vnet.name
#   name = "qfa-${local.env}-db-snet"
#   address_prefixes = ["10.0.2.0/24"]
#   delegation {
#     name = "qfa-${local.env}-db-delegation"
#     service_delegation {
#       name = "Microsoft.DBforPostgreSQL/flexibleServers"
#       actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
#     }
#   }
# }
#
# resource "azurerm_private_dns_zone" "private_dns" {
#   resource_group_name = data.azurerm_resource_group.main.name
#   name = "qfa-${local.env}.postgres.database.azure.com"
# }
#
# resource "azurerm_private_dns_zone_virtual_uetwork_link" "postgres_vnet_link" {
#   name = "qfa-${local.env}-vnet-link"
#   private_dns_zone_name = azurerm_private_dns_zone.private_dns.name
#   virtual_network_id = azurerm_virtual_network.qfa_vnet.id
#   resource_group_name = data.azurerm_resource_group.main.name
# }

# resource "azurerm_postgresql_flexible_server" "db" {
#   location            = data.azurerm_resource_group.main.location
#   name                = "qfa-${local.env}-db"
#   resource_group_name = data.azurerm_resource_group.main.name
#   version = "16"
#   delegated_subnet_id = azurerm_subnet.qfa_db_snet.id
#   private_dns_zone_id = azurerm_private_dns_zone.private_dns.id
#   public_network_access_enabled = false
#   zone = "1"
#
#   storage_mb = 32768
#   storage_tier = "P4"
#
#   sku_name = "B_Standard_B1ms"
#   depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres_vnet_link]
#
#   backup_retention_days = 30
#
#   lifecycle {
#     prevent_destroy = true
#   }
# }
