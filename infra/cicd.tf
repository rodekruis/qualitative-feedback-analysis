# =============================================================================
# GitHub CI/CD
# =============================================================================
# Two least-privilege managed identities are used so that each workflow only
# holds the permissions it actually needs:
#
#   github (deploy identity, AZ_CLIENT_ID)
#     — Used by build/release/promote workflows to push images to ACR and
#       update the App Service container configuration.
#     — Roles: Container Registry Repository Writer (ACR scope),
#              Website Contributor (App Service scope).
#
#   github_terraform (terraform identity, AZ_TERRAFORM_CLIENT_ID)
#     — Used exclusively by terraform.yaml to run `terraform plan/apply`,
#       which creates and updates arbitrary resources in this RG.
#     — Roles: Contributor (RG scope),
#              Storage Blob Data Contributor (tfstate SA scope),
#              Reader on ACR and tfstate SA (so `terraform plan` can refresh
#              azurerm_role_assignment resources at those scopes).
#
# How authentication works:
#   Each identity has a federated identity credential tied to the matching
#   GitHub environment. Workflows authenticate via OIDC — Azure validates the
#   Actions token against the federated credential automatically.

# =============================================================================
# Deploy identity — image builds and App Service deployments
# =============================================================================

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

# Grant GitHub Actions the minimum permissions needed to update the App Service
# container image. Website Contributor scoped to the App Service covers
# `az webapp config container set` without granting any broader RG access.
resource "azurerm_role_assignment" "github_deploy_webapp_contributor" {
  scope                = azurerm_linux_web_app.backend.id
  role_definition_name = "Website Contributor"
  principal_id         = azurerm_user_assigned_identity.github.principal_id
}

resource "azurerm_federated_identity_credential" "github_environment" {
  name                      = "gh-qualitative-feedback-analysis-${local.env}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_repo}:environment:${local.github_environment}"
}

# =============================================================================
# Terraform identity — infrastructure management (terraform plan / apply)
# =============================================================================

resource "azurerm_user_assigned_identity" "github_terraform" {
  name                = local.terraform_managed_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

# terraform.yaml runs `terraform apply` which creates, updates, and deletes
# arbitrary resources in this RG (App Service, Key Vault, VNet, subnets,
# managed identities). Contributor-level breadth is required.
resource "azurerm_role_assignment" "github_terraform_contributor" {
  scope                = data.azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github_terraform.principal_id
}

# Terraform identity needs data-plane access to the Terraform state storage
# account so `terraform init`/`plan`/`apply` in CI can read and write the
# state blob. Scoped to the SA (not the state RG) so the assignment cannot
# accidentally widen if other resources are later added to that RG.
resource "azurerm_role_assignment" "github_terraform_tfstate_blob_contributor" {
  scope                = local.tfstate_sa_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.github_terraform.principal_id
}

# Terraform identity needs to read role assignments at shared-infra scopes so
# that `terraform plan` can refresh the azurerm_role_assignment resources
# defined here and in app_service.tf. The narrower data-plane roles already
# granted (Storage Blob Data Contributor) do not include
# Microsoft.Authorization/roleAssignments/read — only control-plane roles do.
# `Reader` scoped per-resource is the least-privilege fit: it grants `*/read`
# on exactly these two resources, nothing else.
resource "azurerm_role_assignment" "github_terraform_acr_reader" {
  scope                = local.acr_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.github_terraform.principal_id
}

resource "azurerm_role_assignment" "github_terraform_tfstate_reader" {
  scope                = local.tfstate_sa_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.github_terraform.principal_id
}

resource "azurerm_federated_identity_credential" "github_terraform_environment" {
  name                      = "gh-qualitative-feedback-analysis-terraform-${local.env}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_terraform.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_repo}:environment:${local.github_environment}"
}
