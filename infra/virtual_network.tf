# =============================================================================
# Virtual Network
# =============================================================================
# Subnets for specific services live in the respective files.
# E.g., subnet for app service lives in app_service.tf.

resource "azurerm_virtual_network" "qfa_vnet" {
  name                = local.vnet_name
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  address_space       = ["10.0.0.0/16"]
}
