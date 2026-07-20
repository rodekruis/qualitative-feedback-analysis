output "app_url" {
  description = "Default hostname of the App Service"
  value       = "https://${azurerm_linux_web_app.backend.default_hostname}"
}

output "acr_login_server" {
  description = "ACR login server URL"
  value       = "${var.acr_name}.azurecr.io"
}

output "keyvault_uri" {
  description = "Key Vault URI"
  value       = azurerm_key_vault.main.vault_uri
}

output "az_client_id" {
  description = "AZ_CLIENT_ID — client ID of the github_deploy managed identity used by build-from-commit.yaml, release.yaml, _deploy-release.yaml, and promote-to-*.yaml (GitHub-federated OIDC)"
  value       = azurerm_user_assigned_identity.github_deploy.client_id
}

output "az_terraform_client_id" {
  description = "AZ_TERRAFORM_CLIENT_ID — client ID of the github_terraform managed identity. Attach it as a user-assigned identity to the self-hosted runner VM (`az vm identity assign --identities <id>`); terraform.yaml authenticates via the VM's IMDS (ARM_USE_MSI + ARM_CLIENT_ID), not OIDC"
  value       = azurerm_user_assigned_identity.github_terraform.client_id
}

output "postgres_server_fqdn" {
  description = "Private FQDN of the PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server.db.fqdn
}

output "postgres_database_name" {
  description = "Application PostgreSQL database name"
  value       = azurerm_postgresql_flexible_server_database.app.name
}