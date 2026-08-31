variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
}

variable "tenant_id" {
  description = "Azure AD tenant ID"
  type        = string
}

variable "resource_group_name" {
  description = "Where this workspace's environment resources live (App Service, Key Vault, managed identity, etc.). Per-environment in a multi-RG deployment; shared with the other RG roles in a single-RG deployment."
  type        = string
}

variable "acr_resource_group_name" {
  description = "Where the ACR lives. The dedicated platform/shared RG hosting the ACR independently of any environment in a multi-RG deployment; same as resource_group_name in a single-RG deployment."
  type        = string
}

variable "tf_state_storage_account" {
  description = "Globally unique name of the Azure Storage Account holding the Terraform remote state. Must be set explicitly per deployment to avoid name collisions across Azure tenants."
  type        = string
}

variable "tf_state_resource_group_name" {
  description = "RG containing the Terraform state storage account. Separate from resource_group_name so state can live outside any environment RG."
  type        = string
}

variable "acr_name" {
  description = "Globally unique name of the shared Azure Container Registry. Must be set explicitly per deployment to avoid name collisions across Azure tenants. ACR names are alphanumeric only (no dashes)."
  type        = string
}


variable "teams_webhook_url" {
  description = "Microsoft Teams incoming webhook URL that Azure Monitor alerts are POSTed to. Provided via TF_VAR_teams_webhook_url (local shell/.env, or a GitHub Actions secret in CI) — never committed to source or read from Key Vault."
  type        = string
  sensitive   = true
}

variable "github_repo" {
  description = "GitHub repository in owner/name format"
  type        = string
  default     = "rodekruis/qualitative-feedback-analysis"
}

# --- App configuration (non-secret) ---

variable "llm_model" {
  description = "LLM model name"
  type        = string
  default     = "azure/gpt-5.4"
}

variable "llm_api_version" {
  description = "API version for Azure OpenAI and/or Azure AI serverless endpoints"
  type        = string
  default     = "2024-05-01-preview"
}

variable "judge_llm_model" {
  description = "Model for the separate LLM-as-judge connection (JUDGE_LLM_MODEL). An empty string disables the judge connection entirely — judge calls fall back to the primary llm_model, matching pre-judge-config behaviour. This is also the rollback lever: to roll back, set this to \"\" and re-apply, no code change or redeploy needed. See ADR-020."
  type        = string
  default     = "azure_ai/mistral-medium-3-5"
}

variable "judge_llm_api_base" {
  description = "API base for the judge connection (JUDGE_LLM_API_BASE), when it differs from the primary llm route (e.g. azure_ai/ vs azure/). Deliberately left uncommitted (no default) and supplied per-environment via TF_VAR_judge_llm_api_base, since the primary LLM_API_BASE embeds the Azure resource name and is kept out of git via Key Vault for the same reason. Empty means inherit the primary connection's api_base — see docs/operations/settings-reference.md for the inheritance rule."
  type        = string
  default     = ""
  sensitive   = true
}

# --- App Service plan sizing ---

variable "app_service_plan_sku_by_env" {
  description = "App Service plan SKU per Terraform workspace. A workspace missing from this map falls back to B2 (see locals.app_service_plan_sku), so a new environment is never silently provisioned as Premium. prd runs P0v3 because B2 ran close to its memory ceiling under concurrent API calls; see ADR-019."
  type        = map(string)
  default = {
    dev     = "B2"
    staging = "B2"
    prd     = "B3"
  }
}

# --- PostgreSQL configuration ---

variable "postgres_db_name" {
  description = "Application database name"
  type        = string
  default     = "qfa"
}

variable "postgres_sku_name" {
  description = "SKU for PostgreSQL Flexible Server"
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  description = "Storage size in MB for PostgreSQL Flexible Server"
  type        = number
  default     = 32768 #Lowering this number is NOT possible and will lead to an error when running terraform apply.
}

variable "db_aad_scope" {
  description = "AAD scope used by the application to get PostgreSQL access tokens"
  type        = string
  default     = "https://ossrdbms-aad.database.windows.net/.default"
}
