locals {
  env                   = terraform.workspace
  app_name              = "qfa-${local.env}-backend"
  plan_name             = "qfa-${local.env}-plan" # Azure Web Service plan name
  vnet_name             = "qfa-${local.env}-vnet"
  keyvault_name         = "qfa-${local.env}-keyvault"
  managed_identity_name = "qfa-${local.env}-github"
  github_environment    = local.env

  # Resource IDs for shared infra. Constructed deterministically from variables
  # rather than looked up via `data` sources so the CI identity does not need
  # control-plane read on these resources — it only needs the roles it is
  # explicitly granted (ACR push, tfstate blob write, etc.).
  tfstate_sa_id = "/subscriptions/${var.subscription_id}/resourceGroups/${var.tf_state_resource_group_name}/providers/Microsoft.Storage/storageAccounts/${var.tf_state_storage_account}"
  acr_id        = "/subscriptions/${var.subscription_id}/resourceGroups/${var.acr_resource_group_name}/providers/Microsoft.ContainerRegistry/registries/${var.acr_name}"
}
