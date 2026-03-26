# =============================================================================
# Resource Group (read-only — managed outside Terraform)
# =============================================================================

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

# =============================================================================
# Container Registry
# =============================================================================

resource "azurerm_container_registry" "acr" {
  name                = var.acr_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false
}

# =============================================================================
# Key Vault
# =============================================================================

resource "azurerm_key_vault" "main" {
  name                       = var.keyvault_name
  resource_group_name        = data.azurerm_resource_group.main.name
  location                   = data.azurerm_resource_group.main.location
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 90
  purge_protection_enabled   = false
}

# App Service identity: read secrets from Key Vault
resource "azurerm_role_assignment" "app_keyvault_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_linux_web_app.backend.identity[0].principal_id
}

# Key Vault secret names are declared here, but VALUES are managed out-of-band
# (via az keyvault secret set / the update_auth_api_keys.py script).
# This avoids storing secrets in Terraform state.

# =============================================================================
# App Service Plan
# =============================================================================

resource "azurerm_service_plan" "main" {
  name                = var.plan_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = "B1"
}

# =============================================================================
# App Service (Linux container)
# =============================================================================

resource "azurerm_linux_web_app" "backend" {
  name                = var.app_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  service_plan_id     = azurerm_service_plan.main.id
  https_only                            = false
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false

  identity {
    type = "SystemAssigned"
  }

  site_config {
    always_on              = true
    health_check_path                 = "/v1/health"
    health_check_eviction_time_in_min = 10
    http2_enabled          = true
    ftps_state             = "FtpsOnly"
    minimum_tls_version    = "1.2"
    scm_minimum_tls_version = "1.2"

    container_registry_use_managed_identity = true
  }

  app_settings = {
    LLM_PROVIDER    = var.llm_provider
    LLM_MODEL       = var.llm_model
    LLM_API_VERSION = var.llm_api_version

    # Key Vault references — the App Service resolves these at runtime
    LLM_AZURE_ENDPOINT = "@Microsoft.KeyVault(SecretUri=https://${var.keyvault_name}.vault.azure.net/secrets/llm-azure-endpoint)"
    LLM_API_KEY        = "@Microsoft.KeyVault(SecretUri=https://${var.keyvault_name}.vault.azure.net/secrets/llm-api-key)"
    AUTH_API_KEYS      = "@Microsoft.KeyVault(SecretUri=https://${var.keyvault_name}.vault.azure.net/secrets/auth-api-keys)"

    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
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

# Grant the App Service pull access to ACR
resource "azurerm_role_assignment" "app_acr_repository_reader" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "Container Registry Repository Reader"
  principal_id         = azurerm_linux_web_app.backend.identity[0].principal_id
}

# Grant GitHub Actions write access to ACR (for CI/CD image builds)
resource "azurerm_role_assignment" "github_acr_repository_writer" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "Container Registry Repository Writer"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

# =============================================================================
# Managed Identity for GitHub Actions (OIDC)
# =============================================================================

resource "azurerm_user_assigned_identity" "github" {
  name                = var.managed_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

resource "azurerm_federated_identity_credential" "github_production" {
  name      = "gh-qualitative-feedback-analysis-production"
  user_assigned_identity_id = azurerm_user_assigned_identity.github.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "repo:${var.github_repo}:environment:${var.github_environment}"
}

# GitHub Actions identity gets Contributor on the resource group
resource "azurerm_role_assignment" "github_contributor" {
  scope                = data.azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

# =============================================================================
# GitHub: environment + variables
# =============================================================================

resource "github_repository_environment" "production" {
  environment = var.github_environment
  repository  = split("/", var.github_repo)[1]
}

resource "github_actions_environment_variable" "az_client_id" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_CLIENT_ID"
  value         = azurerm_user_assigned_identity.github.client_id
}

resource "github_actions_environment_variable" "az_tenant_id" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_TENANT_ID"
  value         = var.tenant_id
}

resource "github_actions_environment_variable" "az_subscription_id" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_SUBSCRIPTION_ID"
  value         = var.subscription_id
}

resource "github_actions_environment_variable" "az_resource_group" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_RESOURCE_GROUP"
  value         = var.resource_group_name
}

resource "github_actions_environment_variable" "az_app_name" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_APP_NAME"
  value         = var.app_name
}

resource "github_actions_environment_variable" "az_acr_name" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_ACR_NAME"
  value         = var.acr_name
}

resource "github_actions_environment_variable" "az_keyvault" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "AZ_KEYVAULT"
  value         = var.keyvault_name
}

resource "github_actions_environment_variable" "llm_provider" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "LLM_PROVIDER"
  value         = var.llm_provider
}

resource "github_actions_environment_variable" "llm_model" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "LLM_MODEL"
  value         = var.llm_model
}

resource "github_actions_environment_variable" "llm_api_version" {
  repository    = split("/", var.github_repo)[1]
  environment   = github_repository_environment.production.environment
  variable_name = "LLM_API_VERSION"
  value         = var.llm_api_version
}
