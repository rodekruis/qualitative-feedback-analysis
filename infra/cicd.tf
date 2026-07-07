# =============================================================================
# GitHub CI/CD
# =============================================================================
# Allow GitHub actions to read/write to the container registry, and modify the resource group
# (e.g., to manage app service settings etc).
#
# How it works:
# 1. we create a managed identity
# 2. assign roles "Container Registry Repository Writer" and "Contributor" to the managed identity
# 3. add a federated identity credential to the managed identity
#
# Github actions authenticate as the managed identity via the federated identity credential.
# This happens automatically -- Azure "knows" that an action is triggered from the
# repository and environment specified in the federated identity credential.

resource "azurerm_user_assigned_identity" "github" {
  name                = local.managed_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

# Grant GitHub Actions write access to ACR (for CI/CD image builds)
resource "azurerm_role_assignment" "github_acr_repository_writer" {
  scope                = local.acr_id
  role_definition_name = "Container Registry Repository Writer"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

# GitHub Actions identity gets Contributor on the resource group.
#
# Why Contributor and not a narrower role:
#   terraform.yaml runs `terraform apply` which creates, updates, and deletes
#   arbitrary resources in this RG (App Service, Key Vault, VNet, subnets,
#   managed identities). That requires Contributor-level breadth. A scoped
#   role like Website Contributor would only cover the App Service, breaking
#   all other Terraform-managed resources.
#
# When to revisit:
#   If the team grows and you want least-privilege separation, split into two
#   identities: one for Terraform (Contributor on the RG, used only by
#   terraform.yaml) and one for deployment (Website Contributor on the App
#   Service, used by the release/promote workflows). That requires a second
#   managed identity, a second federated credential, and a second set of
#   GitHub environment variables.
resource "azurerm_role_assignment" "github_contributor" {
  scope                = data.azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

# GitHub Actions identity needs data-plane access to the Terraform state
# storage account so `terraform init`/`plan`/`apply` in CI can read and write
# the state blob. Scoped to the SA (not the state RG) so the assignment cannot
# accidentally widen if other resources are later added to that RG.
resource "azurerm_role_assignment" "github_tfstate_blob_contributor" {
  scope                = local.tfstate_sa_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

# CI identity needs to read role assignments at shared-infra scopes so that
# `terraform plan` can refresh the azurerm_role_assignment resources defined
# here and in app_service.tf. The narrower data-plane roles already granted
# (Container Registry Repository Writer, Storage Blob Data Contributor) do
# not include Microsoft.Authorization/roleAssignments/read — only control-
# plane roles do. `Reader` scoped per-resource is the least-privilege fit:
# it grants `*/read` on exactly these two resources, nothing else.
#
# Write-side (apply creating or modifying role assignments at these scopes)
# still requires operator credentials in a local apply. Reader covers the
# steady-state CI plan/apply cycle.
resource "azurerm_role_assignment" "github_acr_reader" {
  scope                = local.acr_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

resource "azurerm_role_assignment" "github_tfstate_reader" {
  scope                = local.tfstate_sa_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

resource "azurerm_federated_identity_credential" "github_environment" {
  name                      = "gh-qualitative-feedback-analysis-${local.env}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_repo}:environment:${local.github_environment}"
}
