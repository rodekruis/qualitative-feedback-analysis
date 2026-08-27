locals {
  env                   = terraform.workspace
  app_name              = "qfa-${local.env}-backend"
  plan_name             = "qfa-${local.env}-plan" # Azure Web Service plan name
  vnet_name             = "qfa-${local.env}-vnet"
  keyvault_name         = "qfa-${local.env}-keyvault"
  managed_identity_name = "qfa-${local.env}-github"
  github_environment    = local.env
  db_aad_principal_name = local.app_name # system-assigned MI name matches the App Service name

  # Premium (Pv3) only where user load justifies it — ADR-019.
  app_service_plan_sku = lookup(var.app_service_plan_sku_by_env, local.env, "B2")

  # Resource IDs for shared infra. Constructed deterministically from variables
  # rather than looked up via `data` sources so the CI identity does not need
  # control-plane read on these resources — it only needs the roles it is
  # explicitly granted (ACR push, tfstate blob write, etc.).
  tfstate_sa_id = "/subscriptions/${var.subscription_id}/resourceGroups/${var.tf_state_resource_group_name}/providers/Microsoft.Storage/storageAccounts/${var.tf_state_storage_account}"
  acr_id        = "/subscriptions/${var.subscription_id}/resourceGroups/${var.acr_resource_group_name}/providers/Microsoft.ContainerRegistry/registries/${var.acr_name}"

  # App settings for the optional judge connection. Built conditionally,
  # not as a static map with possibly-empty values: JudgeLLMSettings treats
  # JUDGE_LLM_API_BASE being absent as "inherit the primary connection's
  # api_base" and being present-but-empty as "override to empty"
  # (src/qfa/settings.py) — writing "" unconditionally when
  # judge_llm_api_base isn't set would silently break every judge call
  # with an empty base URL instead of inheriting the primary one. No Key
  # Vault entry is involved: the judge API key always inherits
  # JUDGE_LLM_API_KEY's default, the primary LLM_API_KEY.
  judge_app_settings = var.judge_llm_model == "" ? {} : merge(
    { JUDGE_LLM_MODEL = var.judge_llm_model },
    var.judge_llm_api_base == "" ? {} : { JUDGE_LLM_API_BASE = var.judge_llm_api_base },
  )
}
