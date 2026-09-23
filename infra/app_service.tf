# =============================================================================
# App Service
# =============================================================================

resource "azurerm_service_plan" "main" {
  name                = local.plan_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = local.app_service_plan_sku
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

  # The system-assigned identity still serves Key Vault references and the ACR
  # pull (both read `identity[0].principal_id` below, which resolves to the
  # system-assigned principal while the type includes it). The user-assigned one
  # exists only for Postgres — see ADR-023.
  identity {
    type         = "SystemAssigned, UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.db_admin.id]
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

  # merge()'d with local.judge_app_settings and local.langfuse_app_settings
  # rather than a static map so an unset judge_llm_model / langfuse_public_key
  # can omit their app settings entirely instead of writing them as empty
  # strings — see the comments on those locals (infra/locals.tf) for why
  # that distinction matters.
  app_settings = merge(
    {
      LLM_MODEL       = var.llm_model
      LLM_API_VERSION = var.llm_api_version

      # Key Vault references — the App Service resolves these at runtime
      LLM_API_BASE  = "@Microsoft.KeyVault(SecretUri=https://${local.keyvault_name}.vault.azure.net/secrets/llm-api-base)"
      LLM_API_KEY   = "@Microsoft.KeyVault(SecretUri=https://${local.keyvault_name}.vault.azure.net/secrets/llm-api-key)"
      AUTH_API_KEYS = "@Microsoft.KeyVault(SecretUri=https://${local.keyvault_name}.vault.azure.net/secrets/auth-api-keys)"

      DB_URL           = ""
      DB_HOST          = azurerm_postgresql_flexible_server.db.fqdn
      DB_PORT          = "5432"
      DB_NAME          = var.postgres_db_name
      DB_AUTH_MODE     = "entra"
      DB_AAD_SCOPE     = var.db_aad_scope
      DB_AAD_CLIENT_ID = azurerm_user_assigned_identity.db_admin.client_id
      DB_USER          = local.db_aad_principal_name

      WEBSITES_ENABLE_APP_SERVICE_STORAGE  = "false"
      WEBSITES_PORT                        = "8000"

      APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.main.connection_string
    },
    local.judge_app_settings,
    local.langfuse_app_settings,
  )

  logs {
    # "Application Logging (Filesystem)" in the Portal. This is what captures
    # the container's stdout/stderr (Python `logging` output) into the
    # AppServiceConsoleLogs category at all — without it there is nothing for
    # the Log stream or the diagnostic setting (observability.tf) to show,
    # regardless of what the app itself logs.
    application_logs {
      file_system_level = "Information"
    }

    http_logs {
      file_system {
        retention_in_days = 3
        retention_in_mb   = 100
      }
    }
  }

  lifecycle {
    # The container image tag is updated by the CI/CD pipeline, not Terraform
    ignore_changes = [
      site_config[0].application_stack,
      # The deploy pipeline writes these tags on every deploy (see
      # .github/workflows/_deploy-release.yaml). Terraform must not manage or
      # revert them, or each apply would wipe the deployed-version record.
      # Scoped to the specific keys (not the whole `tags` map) so any
      # Terraform-managed tags added later are still reconciled.
      tags["deployed_version"],
      tags["deployed_digest"],
      tags["deployed_at"],
      tags["deployed_by"],
    ]
  }

  # The container runs `python -m qfa.cli.migrate` before uvicorn binds the
  # port (entrypoint.sh), so on a fresh environment the Entra admin must exist
  # before the app does or the first boot crash-loops on an unauthorised DB.
  # This edge was impossible while the admin pointed at this app's identity;
  # ADR-023 reversed it.
  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.db]
}

# App Service identity: read secrets from Key Vault.
#
# Both assignments below hang off the *system-assigned* principal, so both are
# replaced whenever the App Service is recreated. `create_before_destroy` keeps
# the old assignment alive until the new one exists — without it Terraform
# destroys first, leaving a window in which the rebuilt app can resolve no Key
# Vault reference and pull no image. Assignment names are provider-generated
# GUIDs, so the overlap cannot collide.
#
# `skip_service_principal_aad_check` suppresses the provider's up-front
# principal lookup: on a first apply the identity is minutes old and Entra
# replication lag makes that check fail with PrincipalNotFound even though the
# principal is valid.
resource "azurerm_role_assignment" "app_keyvault_secrets" {
  scope                            = azurerm_key_vault.main.id
  role_definition_name             = "Key Vault Secrets User"
  principal_id                     = azurerm_linux_web_app.backend.identity[0].principal_id
  skip_service_principal_aad_check = true

  lifecycle {
    create_before_destroy = true
  }
}


# Grant the App Service pull access to ACR
resource "azurerm_role_assignment" "app_acr_repository_reader" {
  scope                            = local.acr_id
  role_definition_name             = "Container Registry Repository Reader"
  principal_id                     = azurerm_linux_web_app.backend.identity[0].principal_id
  skip_service_principal_aad_check = true

  lifecycle {
    create_before_destroy = true
  }
}
