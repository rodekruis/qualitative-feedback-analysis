# =============================================================================
# GitHub CI/CD
# =============================================================================
# Two managed identities, split by blast radius (ADR-021):
#   - "github" (infra): Contributor on the resource group. Used only by
#     terraform.yaml, which needs to create/update/delete arbitrary
#     RG-scoped resources.
#   - "github_deploy": Website Contributor on the App Service only. Used by
#     the release/promote/build-from-commit workflows, which only ever
#     repoint the App Service's container image and tags.
#
# Both authenticate via a federated identity credential -- Azure "knows" an
# action is the identity it claims because the action is triggered from the
# repository and environment named in that identity's credential.

resource "azurerm_user_assigned_identity" "github" {
  name                = local.managed_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
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
# The deploy workflows (release/promote/build-from-commit) no longer use
# this identity -- see azurerm_user_assigned_identity.github_deploy below
# and ADR-021 for why the split was made and what it does and doesn't fix.
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

# Deploy-only identity (ADR-021). Used by release.yaml, build-from-commit.yaml
# and _deploy-release.yaml, which only ever run `az webapp config container
# set` and `az webapp update --set tags.*` against this environment's App
# Service -- never Terraform.
resource "azurerm_user_assigned_identity" "github_deploy" {
  name                = local.deploy_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

# Scoped to the App Service *resource*, not the RG: the deploy path only
# ever runs `az webapp config container set` and `az webapp update --set
# tags.*`. Website Contributor is Microsoft.Web/sites/* (plus
# Microsoft.Authorization/*/read) at that one resource -- it cannot touch
# Postgres, Key Vault, the VNet, or Terraform state. A custom role would be
# narrower still; see ADR-021 for why it was rejected.
resource "azurerm_role_assignment" "github_deploy_website_contributor" {
  scope                = azurerm_linux_web_app.backend.id
  role_definition_name = "Website Contributor"
  principal_id         = azurerm_user_assigned_identity.github_deploy.principal_id
}

# ACR push, dev only. release.yaml's build job and build-from-commit.yaml
# both hardcode `environment: dev`, so no other workspace's deploy identity
# has any reason to write to the shared registry. Reader is required on top:
# Repository Writer is data-actions-only, and `az acr login` /
# `az acr repository show` resolve the registry through ARM first.
resource "azurerm_role_assignment" "github_deploy_acr_repository_writer" {
  count                = local.env == "dev" ? 1 : 0
  scope                = local.acr_id
  role_definition_name = "Container Registry Repository Writer"
  principal_id         = azurerm_user_assigned_identity.github_deploy.principal_id
}

resource "azurerm_role_assignment" "github_deploy_acr_reader" {
  count                = local.env == "dev" ? 1 : 0
  scope                = local.acr_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.github_deploy.principal_id
}

resource "azurerm_federated_identity_credential" "github_deploy_environment" {
  name                      = "gh-qualitative-feedback-analysis-${local.env}-deploy"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_deploy.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_repo}:environment:${local.github_environment}"
}
