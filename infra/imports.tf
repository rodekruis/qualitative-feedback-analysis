# =============================================================================
# Import blocks — adopt existing Azure resources into Terraform state.
#
# Run `terraform plan` to verify these match. Once the first `terraform apply`
# succeeds (importing everything), you can delete this file.
# =============================================================================

import {
  to = azurerm_container_registry.acr
  id = "/subscriptions/${var.subscription_id}/resourceGroups/${var.resource_group_name}/providers/Microsoft.ContainerRegistry/registries/qfacontainerreg"
}
