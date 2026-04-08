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
  resource_group_name  = data.azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.qfa_vnet.name
  name                 = "qfa-${local.env}-backend-snet"
  address_prefixes     = ["10.0.1.0/24"]
  delegation {
    name = "app-service-delegation"
    service_delegation {
      name    = "Microsoft.Web/serverFarms"
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
  virtual_network_subnet_id                      = azurerm_subnet.qfa_backend_snet.id

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



