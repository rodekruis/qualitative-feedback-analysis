# =============================================================================
# Database
# =============================================================================

resource "azurerm_subnet" "qfa_db_snet" {
  resource_group_name  = data.azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.qfa_vnet.name
  name                 = "qfa-${local.env}-db-snet"
  address_prefixes     = ["10.0.2.0/24"]

  delegation {
    name = "qfa-${local.env}-db-delegation"

    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_private_dns_zone" "postgres" {
  resource_group_name = data.azurerm_resource_group.main.name
  name                = "qfa-${local.env}.postgres.database.azure.com"
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres_vnet_link" {
  name                  = "qfa-${local.env}-postgres-vnet-link"
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.qfa_vnet.id
  resource_group_name   = data.azurerm_resource_group.main.name
}

ephemeral "random_password" "postgres_admin" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "azurerm_postgresql_flexible_server" "db" {
  location                      = data.azurerm_resource_group.main.location
  name                          = "qfa-${local.env}-db"
  resource_group_name           = data.azurerm_resource_group.main.name
  version                       = "16"
  delegated_subnet_id           = azurerm_subnet.qfa_db_snet.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  zone                          = "1"

  # Local admin credentials are provisioning-only; runtime app auth is Entra.
  administrator_login    = var.postgres_admin_username
  administrator_password = ephemeral.random_password.postgres_admin.result

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = var.tenant_id
  }

  storage_mb            = var.postgres_storage_mb
  sku_name              = var.postgres_sku_name
  backup_retention_days = 30

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres_vnet_link]

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "db" {
  server_name         = azurerm_postgresql_flexible_server.db.name
  resource_group_name = data.azurerm_resource_group.main.name
  tenant_id           = var.tenant_id
  object_id           = azurerm_linux_web_app.backend.identity[0].principal_id
  principal_name      = local.db_aad_principal_name
  principal_type      = "ServicePrincipal"
}

resource "azurerm_postgresql_flexible_server_database" "app" {
  name      = var.postgres_db_name
  server_id = azurerm_postgresql_flexible_server.db.id
  collation = "en_US.utf8"
  charset   = "UTF8"
}
