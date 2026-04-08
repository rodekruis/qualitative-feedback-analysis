# =============================================================================
# Resource Group (read-only — managed outside Terraform)
# =============================================================================

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}
