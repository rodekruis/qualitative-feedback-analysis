# Infrastructure Bootstrap

This is a **one-time local setup** required before the CI/CD pipeline can manage infrastructure autonomously.

## Why is a local bootstrap needed?

The Terraform configuration in `infra/` manages Azure resources (Key Vault, App Service, managed identity, etc.). CI needs GitHub Actions environment variables to authenticate with Azure, but those variables depend on resources that Terraform creates — so the first apply must be run locally with personal credentials.

Two of the resources Terraform depends on cannot be managed by Terraform itself: the storage account holding Terraform's own state, and the container registry that Terraform reads as a data source. These are the chicken-and-egg dependencies that `bootstrap.sh` creates before `terraform init` can run.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — authenticated (`az login`)
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- [GitHub CLI](https://cli.github.com/) — authenticated (`gh auth login`) with a token that has `repo` scope
- An **Azure resource group** that already exists. Terraform reads it as a data source rather than managing it; create it via the Azure portal or `az group create -n <name> -l westeurope`. Its name will be passed to Terraform via `TF_VAR_resource_group_name` in step 2.

## Steps

### 1. Change into the infra directory

```bash
cd infra
```

### 2. Export environment variables

These are used by `bootstrap.sh`, by Terraform locally, and re-used in step 7 to populate the GitHub Actions variables.

```bash
# Required — Azure tenant + target resource group
export TF_VAR_tenant_id=<your-azure-tenant-id>
export TF_VAR_subscription_id=<your-azure-subscription-id>
export TF_VAR_resource_group_name=<your-resource-group-name>

# Required — globally unique Azure resource names. They must not collide
# with any other Storage Account / Container Registry in any Azure tenant.
# Pick names tied to your deployment, e.g. <orgshort>tfstate / <orgshort>acr.
# ACR names allow alphanumeric only (no dashes).
export TF_VAR_tf_state_storage_account=<globally-unique-storage-account-name>
export TF_VAR_acr_name=<globally-unique-acr-name>

# Optional — Azure region for the bootstrapped resources. Defaults to westeurope.
export LOCATION=westeurope
```

### 3. Create the chicken-and-egg resources

**NOTE:** Run this step only once per deployment. If the storage account and container registry already exist, skip it.

These two resources have to exist before `terraform init` can run, because Terraform itself depends on them:

- **Azure Storage Account** (`$TF_VAR_tf_state_storage_account`) — the Terraform remote state backend. Globally unique.
- **Container Registry** (`$TF_VAR_acr_name`) — referenced by Terraform as a `data` source. Globally unique. Used by all environments to store and pull container images.

Both are created in `$TF_VAR_resource_group_name`.

```bash
bash bootstrap.sh
```

### 4. Initialize Terraform

Terraform's `backend` block does not allow variable interpolation, so the resource group and storage account names must be supplied at `terraform init` time via `-backend-config` flags:

```bash
terraform init \
  -backend-config="resource_group_name=$TF_VAR_resource_group_name" \
  -backend-config="storage_account_name=$TF_VAR_tf_state_storage_account"
```

If `terraform init` fails with an Azure CLI authorizer or tenant ID error, make sure you are successfully logged in to Azure:

```bash
az login
```

Then re-run the `terraform init` command above.

### 5. Create workspaces

Terraform uses workspaces to manage `dev` and `prd` environments with separate state files.

```bash
terraform workspace new dev
terraform workspace new prd
```

### 6. Apply for each environment

```bash
# Dev environment
terraform workspace select dev
terraform apply

# Production environment
terraform workspace select prd
terraform apply
```

### 7. Create GitHub environments and set variables

After both applies complete, create the GitHub Actions environments and populate their variables. The `AZ_CLIENT_ID` is environment-specific (it comes from the managed identity Terraform just created); the rest are static.

```bash
REPO="rodekruis/qualitative-feedback-analysis"

# --- dev ---
gh api repos/$REPO/environments/dev -X PUT

terraform workspace select dev
gh variable set AZ_CLIENT_ID       --env dev --repo $REPO --body "$(terraform output -raw az_client_id)"
gh variable set AZ_TENANT_ID       --env dev --repo $REPO --body "$TF_VAR_tenant_id"
gh variable set AZ_SUBSCRIPTION_ID --env dev --repo $REPO --body "$TF_VAR_subscription_id"
gh variable set AZ_RESOURCE_GROUP  --env dev --repo $REPO --body "$TF_VAR_resource_group_name"
gh variable set AZ_APP_NAME        --env dev --repo $REPO --body "qfa-dev-backend"
gh variable set AZ_ACR_NAME        --env dev --repo $REPO --body "$TF_VAR_acr_name"

# --- prd ---
gh api repos/$REPO/environments/prd -X PUT

terraform workspace select prd
gh variable set AZ_CLIENT_ID       --env prd --repo $REPO --body "$(terraform output -raw az_client_id)"
gh variable set AZ_TENANT_ID       --env prd --repo $REPO --body "$TF_VAR_tenant_id"
gh variable set AZ_SUBSCRIPTION_ID --env prd --repo $REPO --body "$TF_VAR_subscription_id"
gh variable set AZ_RESOURCE_GROUP  --env prd --repo $REPO --body "$TF_VAR_resource_group_name"
gh variable set AZ_APP_NAME        --env prd --repo $REPO --body "qfa-prd-backend"
gh variable set AZ_ACR_NAME        --env prd --repo $REPO --body "$TF_VAR_acr_name"
```

The `terraform.yaml` workflow can now run autonomously in CI.

## Subsequent infrastructure changes

After the bootstrap, infrastructure changes follow the normal workflow:

- Open a PR touching `infra/` → CI runs `terraform plan` automatically
- Merge to `main` → trigger `terraform apply` manually from the Actions tab

If the managed identity is ever recreated (e.g. after `terraform destroy`), re-run step 7 for the affected environment to update `AZ_CLIENT_ID`.
