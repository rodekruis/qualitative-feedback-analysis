# =============================================================================
# Container Registry — referenced by constructed resource ID
# =============================================================================
# The ACR is managed outside Terraform (see bootstrap.sh) and lives in a
# shared RG. We do not use a `data "azurerm_container_registry"` block because
# that would require the CI identity to hold control-plane read on the ACR,
# widening its role footprint beyond the data-plane `Container Registry
# Repository Writer` it actually needs. Instead, the ACR's resource ID is
# constructed deterministically in locals.tf (`local.acr_id`) from
# var.subscription_id, var.acr_resource_group_name, and var.acr_name.
#
# Role assignments referencing ACR:
#   - azurerm_role_assignment.github_acr_repository_writer (cicd.tf)
#   - azurerm_role_assignment.app_acr_repository_reader    (app_service.tf)
