variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
}

variable "tenant_id" {
  description = "Azure AD tenant ID"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group"
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
