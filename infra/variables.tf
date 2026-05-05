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


variable "github_repo" {
  description = "GitHub repository in owner/name format"
  type        = string
  default     = "rodekruis/qualitative-feedback-analysis"
}

# --- App configuration (non-secret) ---

variable "llm_model" {
  description = "LLM model name"
  type        = string
  default     = "azure_ai/mistral-medium-2505"
}

variable "llm_api_version" {
  description = "API version for Azure OpenAI and/or Azure AI serverless endpoints"
  type        = string
  default     = "2024-05-01-preview"
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
  default     = 32768
}

variable "db_aad_scope" {
  description = "AAD scope used by the application to get PostgreSQL access tokens"
  type        = string
  default     = "https://ossrdbms-aad.database.windows.net/.default"
}
