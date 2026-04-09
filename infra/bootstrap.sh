#!/usr/bin/env bash
# Bootstrap the Azure Storage Account used as Terraform remote backend.
# Run this ONCE before `terraform init`. This is the one resource that
# lives outside of Terraform's management (chicken-and-egg problem).
set -euo pipefail

# Three resource group roles, possibly distinct in a multi-RG deployment, all
# the same in a single-RG deployment:
#   * tf_state_resource_group_name — where the state storage account lives
#   * acr_resource_group_name      — where the ACR lives
#   * resource_group_name          — where Terraform creates environment resources
#
# bootstrap.sh only creates the state SA and the ACR. The environment RG is
# Terraform's concern and is not touched here. The TF_VAR_* env vars are also
# consumed by Terraform itself; exporting them once keeps bootstrap.sh and the
# Terraform plan in sync. The `${VAR:?msg}` form fails immediately with a clear
# message if the variable is unset or empty, preventing partial-name resource
# creation.
TF_STATE_RG="${TF_VAR_tf_state_resource_group_name:?must be set: export TF_VAR_tf_state_resource_group_name=<rg-where-state-lives>}"
ACR_RG="${TF_VAR_acr_resource_group_name:?must be set: export TF_VAR_acr_resource_group_name=<rg-where-acr-lives>}"
SA="${TF_VAR_tf_state_storage_account:?must be set: export TF_VAR_tf_state_storage_account=<globally-unique-storage-account-name>}"
ACR="${TF_VAR_acr_name:?must be set: export TF_VAR_acr_name=<globally-unique-acr-name-alphanumeric-only>}"
CONTAINER="tfstate"
LOCATION="${LOCATION:-westeurope}"

echo "Creating storage account for Terraform state in $TF_STATE_RG..."
az storage account create \
  --name "$SA" \
  --resource-group "$TF_STATE_RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false

echo "Creating blob container..."
az storage container create \
  --name "$CONTAINER" \
  --account-name "$SA" \
  --auth-mode login

echo "Creating container registry in $ACR_RG..."
az acr create \
  --name "$ACR" \
  --resource-group "$ACR_RG" \
  --location "$LOCATION" \
  --sku Basic \
  --admin-enabled false

echo "Done. Now run: cd infra && terraform init"
