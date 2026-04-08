# =============================================================================
# Container Registry (read-only — managed outside Terraform)
# =============================================================================

data "azurerm_container_registry" "acr" {
  name                = local.acr_name
  resource_group_name = data.azurerm_resource_group.main.name
}
