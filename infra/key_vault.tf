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
  purge_protection_enabled   = true
}

# Key Vault secret names are declared here, but VALUES are managed out-of-band
# (via az keyvault secret set / the update_auth_api_keys.py script).
# This avoids storing secrets in Terraform state.
#
# Secrets expected in this vault:
#   - llm-api-base            (app_service.tf, read via @Microsoft.KeyVault app_settings resolver)
#   - llm-api-key              (app_service.tf, read via @Microsoft.KeyVault app_settings resolver)
#   - auth-api-keys            (app_service.tf, read via @Microsoft.KeyVault app_settings resolver)
#   - teams-alerts-webhook-url (observability.tf, read via a `data` source at
#                               plan/apply time — the only secret whose value
#                               is materialized into Terraform state; see the
#                               comment above data.azurerm_key_vault_secret.teams_webhook)
