# =============================================================================
# GitHub CI/CD
# =============================================================================
# Two identities, split by blast radius (#80) and network reachability (#176):
#
# github_terraform: Contributor on the RG + tfstate data-plane access.
#   Used only by terraform.yaml, which needs to create/update/delete arbitrary
#   RG resources. Attached as a user-assigned identity to the self-hosted
#   runner VM that lives inside qfa_vnet (see runner_snet below) and
#   authenticates via that VM's IMDS (ARM_USE_MSI), not GitHub OIDC — the
#   runner has no route to the public internet once the tfstate storage
#   account network lockdown (infra/lockdown-tfstate-network.sh) is applied.
#
# github_deploy: narrow, deployment-only permissions (ACR push, Website
#   Contributor on the App Service only). Used by build-from-commit.yaml,
#   release.yaml, _deploy-release.yaml, and the promote-to-* workflows, all of
#   which keep running on ubuntu-latest via GitHub-federated OIDC exactly as
#   before — none of them touch tfstate.
#
# When to revisit:
#   If a third workflow needs RG-level access, resist folding it into either
#   identity above — give it its own scoped role instead, or the blast-radius
#   narrowing this split exists for erodes again.

resource "azurerm_user_assigned_identity" "github_terraform" {
  name                = local.managed_identity_terraform_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

resource "azurerm_user_assigned_identity" "github_deploy" {
  name                = local.managed_identity_deploy_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

# --- github_terraform: terraform.yaml only ---------------------------------

# terraform.yaml runs `terraform apply`, which creates, updates, and deletes
# arbitrary resources in this RG (App Service, Key Vault, VNet, subnets,
# managed identities). That requires Contributor-level breadth. A scoped role
# like Website Contributor would only cover the App Service, breaking all
# other Terraform-managed resources.
resource "azurerm_role_assignment" "github_terraform_contributor" {
  scope                = data.azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github_terraform.principal_id
}

# Data-plane access to the Terraform state storage account so
# `terraform init`/`plan`/`apply` in CI can read and write the state blob.
# Scoped to the SA (not the state RG) so the assignment cannot accidentally
# widen if other resources are later added to that RG.
resource "azurerm_role_assignment" "github_terraform_tfstate_blob_contributor" {
  scope                = local.tfstate_sa_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.github_terraform.principal_id
}

# CI identity needs to read role assignments at shared-infra scopes so that
# `terraform plan` can refresh the azurerm_role_assignment resources defined
# here and in app_service.tf. The narrower data-plane role already granted
# (Storage Blob Data Contributor) does not include
# Microsoft.Authorization/roleAssignments/read — only control-plane roles do.
# `Reader` scoped per-resource is the least-privilege fit: it grants `*/read`
# on exactly these two resources, nothing else.
#
# Write-side (apply creating or modifying role assignments at these scopes)
# still requires operator credentials in a local apply. Reader covers the
# steady-state CI plan/apply cycle.
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

# No federated_identity_credential for github_terraform: it authenticates via
# the self-hosted runner VM's IMDS (user-assigned identity attached to the
# VM), not OIDC. See infra/README (runner setup) for the manual `az vm
# identity assign` step — Terraform does not manage the runner VM itself,
# the same way bootstrap.sh's resources live outside Terraform.

# --- github_deploy: build-from-commit / release / _deploy-release / promote-to-* ---

# Grant GitHub Actions write access to ACR (for CI/CD image builds)
resource "azurerm_role_assignment" "github_deploy_acr_repository_writer" {
  scope                = local.acr_id
  role_definition_name = "Container Registry Repository Writer"
  principal_id         = azurerm_user_assigned_identity.github_deploy.principal_id
}

# Scoped to the App Service only — these workflows update container image
# and tags, nothing else in the RG.
resource "azurerm_role_assignment" "github_deploy_website_contributor" {
  scope                = azurerm_linux_web_app.backend.id
  role_definition_name = "Website Contributor"
  principal_id         = azurerm_user_assigned_identity.github_deploy.principal_id
}

resource "azurerm_federated_identity_credential" "github_deploy_environment" {
  name                      = "gh-qualitative-feedback-analysis-deploy-${local.env}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_deploy.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_repo}:environment:${local.github_environment}"
}
