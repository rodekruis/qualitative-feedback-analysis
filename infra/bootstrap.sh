#!/usr/bin/env bash
# Bootstrap the Azure Storage Account used as Terraform remote backend.
# Run this ONCE before `terraform init`. This is the one resource that
# lives outside of Terraform's management (chicken-and-egg problem).
set -euo pipefail

# All three TF_VAR_* values are also consumed by Terraform itself; exporting
# them once and reusing them here keeps bootstrap.sh and the Terraform plan
# in sync. The `${VAR:?msg}` form fails immediately with a clear message if
# the variable is unset or empty, preventing partial-name resource creation.
RG="${TF_VAR_resource_group_name:?must be set: export TF_VAR_resource_group_name=<your-rg-name>}"
SA="${TF_VAR_tf_state_storage_account:?must be set: export TF_VAR_tf_state_storage_account=<globally-unique-storage-account-name>}"
ACR="${TF_VAR_acr_name:?must be set: export TF_VAR_acr_name=<globally-unique-acr-name-alphanumeric-only>}"
CONTAINER="tfstate"
LOCATION="${LOCATION:-westeurope}"

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
