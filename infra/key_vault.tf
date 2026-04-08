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
