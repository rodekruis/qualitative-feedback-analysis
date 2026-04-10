locals {
  env                   = terraform.workspace
  app_name              = "qfa-${local.env}-backend"
  plan_name             = "qfa-${local.env}-plan" # Azure Web Service plan name
  vnet_name             = "qfa-${local.env}-vnet"
  keyvault_name         = "qfa-${local.env}-keyvault"
  managed_identity_name = "qfa-${local.env}-github"
  github_environment    = local.env
}
