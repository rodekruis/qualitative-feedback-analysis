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
  description = "AZ_CLIENT_ID — client ID of the managed identity used by GitHub Actions (OIDC)"
  value       = azurerm_user_assigned_identity.github.client_id
}

output "postgres_server_fqdn" {
  description = "Private FQDN of the PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server.db.fqdn
}

output "postgres_database_name" {
  description = "Application PostgreSQL database name"
  value       = azurerm_postgresql_flexible_server_database.app.name
}
