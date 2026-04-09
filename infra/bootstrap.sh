#!/usr/bin/env bash
# Bootstrap the Azure Storage Account used as Terraform remote backend.
# Run this ONCE before `terraform init`. This is the one resource that
# lives outside of Terraform's management (chicken-and-egg problem).
set -euo pipefail

RG=$TF_VAR_resource_group_name
SA="qfatfstate"
CONTAINER="tfstate"
ACR="qfacontainerreg"
LOCATION="westeurope"

echo "Creating storage account for Terraform state..."
az storage account create \
  --name "$SA" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false

echo "Creating blob container..."
az storage container create \
  --name "$CONTAINER" \
  --account-name "$SA" \
  --auth-mode login

echo "Creating container registry..."
az acr create \
  --name "$ACR" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Basic \
  --admin-enabled false

echo "Done. Now run: cd infra && terraform init"
