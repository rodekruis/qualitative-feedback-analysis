# =============================================================================
# Database
# =============================================================================
# resource "azurerm_subnet" "qfa_db_snet" {
#   resource_group_name = data.azurerm_resource_group.main.name
#   virtual_network_name = azurerm_virtual_network.qfa_vnet.name
#   name = "qfa-${local.env}-db-snet"
#   address_prefixes = ["10.0.2.0/24"]
#   delegation {
#     name = "qfa-${local.env}-db-delegation"
#     service_delegation {
#       name = "Microsoft.DBforPostgreSQL/flexibleServers"
#       actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
#     }
#   }
# }
#
# resource "azurerm_private_dns_zone" "private_dns" {
#   resource_group_name = data.azurerm_resource_group.main.name
#   name = "qfa-${local.env}.postgres.database.azure.com"
# }
#
# resource "azurerm_private_dns_zone_virtual_uetwork_link" "postgres_vnet_link" {
#   name = "qfa-${local.env}-vnet-link"
#   private_dns_zone_name = azurerm_private_dns_zone.private_dns.name
#   virtual_network_id = azurerm_virtual_network.qfa_vnet.id
#   resource_group_name = data.azurerm_resource_group.main.name
# }

# resource "azurerm_postgresql_flexible_server" "db" {
#   location            = data.azurerm_resource_group.main.location
#   name                = "qfa-${local.env}-db"
#   resource_group_name = data.azurerm_resource_group.main.name
#   version = "16"
#   delegated_subnet_id = azurerm_subnet.qfa_db_snet.id
#   private_dns_zone_id = azurerm_private_dns_zone.private_dns.id
#   public_network_access_enabled = false
#   zone = "1"
#
#   storage_mb = 32768
#   storage_tier = "P4"
#
#   sku_name = "B_Standard_B1ms"
#   depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres_vnet_link]
#
#   backup_retention_days = 30
#
#   lifecycle {
#     prevent_destroy = true
#   }
# }
