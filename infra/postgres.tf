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

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres_vnet_link" {
  name                  = "qfa-${local.env}-postgres-vnet-link"
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.qfa_vnet.id
  resource_group_name   = data.azurerm_resource_group.main.name
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

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = var.tenant_id
  }
  # Azure does not allow lowering storage - only increases are accepted.
  storage_mb            = var.postgres_storage_mb
  sku_name              = var.postgres_sku_name
  backup_retention_days = 30

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres_vnet_link]

  lifecycle {
    prevent_destroy = true
  }
}

# Dedicated Entra identity for the database admin role (ADR-023). Its principal
# ID survives an App Service rebuild, which the App Service system-assigned
# identity's does not — that mismatch is what locked the app out of its own DB
# (#177). `prevent_destroy` for the same reason as the server itself: the
# in-database role is bound to this principal ID, and re-creating the identity
# means re-granting by hand.
resource "azurerm_user_assigned_identity" "db_admin" {
  name                = local.db_identity_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "db" {
  server_name         = azurerm_postgresql_flexible_server.db.name
  resource_group_name = data.azurerm_resource_group.main.name
  tenant_id           = var.tenant_id
  object_id           = azurerm_user_assigned_identity.db_admin.principal_id
  principal_name      = local.db_aad_principal_name
  principal_type      = "ServicePrincipal"
}

resource "azurerm_postgresql_flexible_server_database" "app" {
  name      = var.postgres_db_name
  server_id = azurerm_postgresql_flexible_server.db.id
  collation = "en_US.utf8"
  charset   = "UTF8"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [name, collation, charset]
  }
}
