# =============================================================================
# Virtual Network
# =============================================================================
# Subnets for specific services live in the respective files.
# E.g., subnet for app service lives in app_service.tf.

resource "azurerm_virtual_network" "qfa_vnet" {
  name                = local.vnet_name
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  address_space       = ["10.0.0.0/16"]
}

# Self-hosted GitHub Actions runner for terraform.yaml (#176). The VM itself
# is not Terraform-managed (chicken-and-egg: it's what reaches the tfstate
# backend), same pattern as bootstrap.sh's resources — see
# infra/lockdown-tfstate-network.sh and the PR runbook for the manual setup.
resource "azurerm_subnet" "qfa_runner_snet" {
  resource_group_name  = data.azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.qfa_vnet.name
  name                 = "qfa-${local.env}-runner-snet"
  address_prefixes     = ["10.0.3.0/24"]
}

# Private endpoint for the tfstate storage account's blob service. Also
# created outside Terraform (the SA itself is), by
# infra/lockdown-tfstate-network.sh. `private_endpoint_network_policies`
# disabled is required for a subnet that hosts private endpoints.
resource "azurerm_subnet" "qfa_tfstate_pe_snet" {
  resource_group_name               = data.azurerm_resource_group.main.name
  virtual_network_name              = azurerm_virtual_network.qfa_vnet.name
  name                              = "qfa-${local.env}-tfstate-pe-snet"
  address_prefixes                  = ["10.0.4.0/24"]
  private_endpoint_network_policies = "Disabled"
}
