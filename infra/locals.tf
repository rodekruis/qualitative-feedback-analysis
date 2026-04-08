locals {
  env                   = terraform.workspace
  app_name              = "qfa-${local.env}-backend"
  plan_name             = "qfa-${local.env}-plan" # Azure Web Service plan name
  acr_name              = "qfacontainerreg"       # does not support dashes
  vnet_name             = "qfa-${local.env}-vnet" # does not support dashes
  keyvault_name         = "qfa-${local.env}-keyvault"
  managed_identity_name = "qfa-${local.env}-github"
  github_environment    = local.env # == "prd" ? "prd" : "dev"
}
