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

variable "llm_provider" {
  description = "LLM provider identifier"
  type        = string
  default     = "azure_openai"
}

variable "llm_model" {
  description = "LLM model name"
  type        = string
  default     = "gpt-4.1-mini"
}

variable "llm_api_version" {
  description = "Azure OpenAI API version"
  type        = string
  default     = "2025-04-01-preview"
}
