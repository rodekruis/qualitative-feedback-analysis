# =============================================================================
# Import blocks — adopt existing Azure resources into Terraform state.
#
# Run `terraform plan` to verify these match. Once the first `terraform apply`
# succeeds (importing everything), you can delete this file.
# =============================================================================

import {
  to = azurerm_resource_group.main
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourceGroups/qualitative-feedback-analysis-xomnia"
}

import {
  to = azurerm_container_registry.acr
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourceGroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.ContainerRegistry/registries/qfacontainerreg"
}

import {
  to = azurerm_key_vault.main
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourceGroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.KeyVault/vaults/rc510-qfa-test-keyvault"
}

import {
  to = azurerm_service_plan.main
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourceGroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.Web/serverfarms/qfa-plan"
}

import {
  to = azurerm_linux_web_app.backend
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourceGroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.Web/sites/qfa-backend"
}

import {
  to = azurerm_user_assigned_identity.github
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourcegroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.ManagedIdentity/userAssignedIdentities/github-workflows"
}

import {
  to = azurerm_federated_identity_credential.github_production
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourcegroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.ManagedIdentity/userAssignedIdentities/github-workflows/federatedIdentityCredentials/gh-qualitative-feedback-analysis-production"
}

import {
  to = azurerm_role_assignment.github_contributor
  id = "/subscriptions/3cea98f3-66ab-4689-b2fc-424a0873f148/resourceGroups/qualitative-feedback-analysis-xomnia/providers/Microsoft.Authorization/roleAssignments/64e94cbc-6d47-42c0-83c0-1b4d55cb054f"
}

# GitHub resources — these use the GitHub API resource path format
import {
  to = github_repository_environment.production
  id = "qualitative-feedback-analysis:production"
}

import {
  to = github_actions_environment_variable.az_client_id
  id = "qualitative-feedback-analysis:production:AZ_CLIENT_ID"
}

import {
  to = github_actions_environment_variable.az_tenant_id
  id = "qualitative-feedback-analysis:production:AZ_TENANT_ID"
}

import {
  to = github_actions_environment_variable.az_subscription_id
  id = "qualitative-feedback-analysis:production:AZ_SUBSCRIPTION_ID"
}

import {
  to = github_actions_environment_variable.az_resource_group
  id = "qualitative-feedback-analysis:production:AZ_RESOURCE_GROUP"
}

import {
  to = github_actions_environment_variable.az_app_name
  id = "qualitative-feedback-analysis:production:AZ_APP_NAME"
}

import {
  to = github_actions_environment_variable.az_acr_name
  id = "qualitative-feedback-analysis:production:AZ_ACR_NAME"
}

import {
  to = github_actions_environment_variable.az_keyvault
  id = "qualitative-feedback-analysis:production:AZ_KEYVAULT"
}

import {
  to = github_actions_environment_variable.llm_provider
  id = "qualitative-feedback-analysis:production:LLM_PROVIDER"
}

import {
  to = github_actions_environment_variable.llm_model
  id = "qualitative-feedback-analysis:production:LLM_MODEL"
}

import {
  to = github_actions_environment_variable.llm_api_version
  id = "qualitative-feedback-analysis:production:LLM_API_VERSION"
}
