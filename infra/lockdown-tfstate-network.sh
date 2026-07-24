#!/usr/bin/env bash
# Locks the Terraform state storage account down to a private endpoint only
# (#176). Run this ONCE, manually, and only after:
#   1. `terraform apply` has run at least once with the github_terraform /
#      github_deploy identity split (infra/cicd.tf) and the runner/PE subnets
#      (infra/virtual_network.tf) already live.
#   2. The self-hosted runner VM exists, has the github_terraform
#      user-assigned identity attached, and has successfully run a
#      terraform.yaml plan against the still-public storage account.
#
# Until step 2 is verified, do NOT run this script — it cuts off every path
# to the state account except the private endpoint created here, and
# ubuntu-latest-hosted CI has no route to it.
#
# Like bootstrap.sh, this manages a resource (the state SA) that lives
# outside Terraform's management — kept as a separate script rather than
# folded into bootstrap.sh because bootstrap.sh is safe to re-run and this
# is not: re-running the network-rule step after operator IPs have changed
# requires deliberate review, not a blind rerun.
set -euo pipefail

TF_STATE_RG="${TF_VAR_tf_state_resource_group_name:?must be set: export TF_VAR_tf_state_resource_group_name=<rg-where-state-lives>}"
SA="${TF_VAR_tf_state_storage_account:?must be set: export TF_VAR_tf_state_storage_account=<state-storage-account-name>}"
RG="${TF_VAR_resource_group_name:?must be set: export TF_VAR_resource_group_name=<rg-where-vnet-lives>}"
VNET_NAME="${VNET_NAME:?must be set: export VNET_NAME=<qfa-<env>-vnet, e.g. qfa-dev-vnet>}"
PE_SUBNET_NAME="${PE_SUBNET_NAME:?must be set: export PE_SUBNET_NAME=<qfa-<env>-tfstate-pe-snet>}"
OPERATOR_IPS="${OPERATOR_IPS:-}" # space-separated list, e.g. "1.2.3.4 5.6.7.8"

echo "Creating private endpoint for $SA in $VNET_NAME/$PE_SUBNET_NAME..."
az network private-endpoint create \
  --name "pe-${SA}-blob" \
  --resource-group "$RG" \
  --vnet-name "$VNET_NAME" \
  --subnet "$PE_SUBNET_NAME" \
  --private-connection-resource-id "$(az storage account show --name "$SA" --resource-group "$TF_STATE_RG" --query id -o tsv)" \
  --group-id blob \
  --connection-name "pe-${SA}-blob-connection"

echo "Creating private DNS zone and linking it to $VNET_NAME..."
az network private-dns zone create \
  --resource-group "$RG" \
  --name "privatelink.blob.core.windows.net"

az network private-dns link vnet create \
  --resource-group "$RG" \
  --zone-name "privatelink.blob.core.windows.net" \
  --name "${VNET_NAME}-tfstate-link" \
  --virtual-network "$VNET_NAME" \
  --registration-enabled false

az network private-endpoint dns-zone-group create \
  --resource-group "$RG" \
  --endpoint-name "pe-${SA}-blob" \
  --name default \
  --private-dns-zone "privatelink.blob.core.windows.net" \
  --zone-name blob

echo "Denying public network access on $SA, with an allowlist for operator IPs..."
az storage account update \
  --name "$SA" \
  --resource-group "$TF_STATE_RG" \
  --default-action Deny \
  --public-network-access Enabled # keep Enabled + Deny-by-default so the IP rules below still apply; only the private endpoint bypasses network rules entirely

if [ -n "$OPERATOR_IPS" ]; then
  for ip in $OPERATOR_IPS; do
    echo "Allowing operator IP $ip..."
    az storage account network-rule add \
      --account-name "$SA" \
      --resource-group "$TF_STATE_RG" \
      --ip-address "$ip"
  done
else
  echo "::warning:: No OPERATOR_IPS set — local 'terraform apply'/'plan' will fail until an operator IP is allowlisted."
fi

echo "Done. Verify: a terraform.yaml run on the self-hosted runner still succeeds,"
echo "and that a run on ubuntu-latest (if you still have one to compare against) now fails."
