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

output "postgres_admin_identity_name" {
  description = "Name of the dedicated identity that is the PostgreSQL Entra admin — also the in-database role name"
  value       = azurerm_user_assigned_identity.db_admin.name
}

output "postgres_admin_identity_principal_id" {
  description = "Object ID of the PostgreSQL Entra admin identity — the OID `pgaadauth_create_principal_with_oid` needs"
  value       = azurerm_user_assigned_identity.db_admin.principal_id
}
