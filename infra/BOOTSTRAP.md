# Infrastructure Bootstrap

This is a **one-time local setup** required before the CI/CD pipeline can manage infrastructure autonomously.

## Why is a local bootstrap needed?

The Terraform configuration in `infra/` manages Azure resources (Key Vault, App Service, managed identity, etc.). CI needs GitHub Actions environment variables to authenticate with Azure, but those variables depend on resources that Terraform creates — so the first apply must be run locally with personal credentials.

Two of the resources Terraform depends on cannot be managed by Terraform itself: the storage account holding Terraform's own state, and the container registry that Terraform reads as a data source. These are the chicken-and-egg dependencies that `bootstrap.sh` creates before `terraform init` can run.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — authenticated (`az login`)
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- [GitHub CLI](https://cli.github.com/) — authenticated (`gh auth login`) with a token that has `repo` scope
- **Azure resource groups** that already exist. You will need:
  - One **per environment** (dev, staging, prd) for that environment's Terraform-managed resources (App Service, Key Vault, managed identity, etc.). Their names are passed to Terraform via `TF_VAR_resource_group_name` in step 6, one at a time.
  - One for the **Terraform state storage account** — passed via `TF_VAR_tf_state_resource_group_name` in step 2. May be the same as one of the env RGs or its own.
  - One for the **shared container registry** — passed via `TF_VAR_acr_resource_group_name` in step 2. May be the same as one of the env RGs or its own.

  For a minimal single-RG deployment, all five roles can point at the same RG. For multi-RG isolation, give each role its own RG. Create each via the Azure portal or `az group create -n <name> -l westeurope`.

## Steps

### 1. Change into the infra directory

```bash
cd infra
```

### 2. Export environment variables

These are read by `bootstrap.sh` (step 3) and by Terraform (from step 4 onwards). They are set **once** and stay constant across all environments. The per-environment `TF_VAR_resource_group_name` is *not* set here — it changes per environment and is exported in step 6, one environment at a time.

```bash
# Required — Azure tenant
export TF_VAR_tenant_id=<your-azure-tenant-id>
export TF_VAR_subscription_id=<your-azure-subscription-id>

# Required — RGs hosting the shared chicken-and-egg infrastructure:
#   * tf_state_resource_group_name — where the Terraform state SA lives
#   * acr_resource_group_name      — where the ACR lives
#
# tf_state_resource_group_name is used only by bootstrap.sh and by the
# `terraform init` command in step 4 (it cannot be a Terraform variable
# because backend blocks forbid variable interpolation). acr_resource_group_name
# is read by Terraform via container_registry.tf.
#
# For a single-RG deployment, point both at the same RG. For a multi-RG
# deployment, point each at its dedicated RG.
export TF_VAR_tf_state_resource_group_name=<rg-where-state-lives>
export TF_VAR_acr_resource_group_name=<rg-where-acr-lives>

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

- **Azure Storage Account** (`$TF_VAR_tf_state_storage_account`) — the Terraform remote state backend. Globally unique. Created in `$TF_VAR_tf_state_resource_group_name`. `bootstrap.sh` also adds a `CannotDelete` lock on the storage account so an accidental `az storage account delete` cannot wipe Terraform state. Removing the lock later requires `Owner` or `User Access Administrator` on the resource.
- **Container Registry** (`$TF_VAR_acr_name`) — referenced by Terraform as a `data` source. Globally unique. Created in `$TF_VAR_acr_resource_group_name`. Used by all environments to store and pull container images.

```bash
bash bootstrap.sh
```

### 4. Initialize Terraform

Terraform's `backend` block does not allow variable interpolation, so the resource group and storage account names must be supplied at `terraform init` time via `-backend-config` flags:

```bash
terraform init \
  -backend-config="resource_group_name=$TF_VAR_tf_state_resource_group_name" \
  -backend-config="storage_account_name=$TF_VAR_tf_state_storage_account"
```

If `terraform init` fails with an Azure CLI authorizer or tenant ID error, make sure you are successfully logged in to Azure:

```bash
az login
```

Then re-run the `terraform init` command above.
If it still fails, make sure your roles are activated in the Azure portal.

### 5. Create workspaces

Terraform uses [workspaces](https://developer.hashicorp.com/terraform/language/state/workspaces)
to manage `dev`, `staging`, and `prd` environments with separate state files.

```bash
terraform workspace new dev
terraform workspace new staging
terraform workspace new prd
```

### 6. Apply each environment and configure its GitHub variables

This is the per-environment loop. Each environment lives in its own resource group, so `TF_VAR_resource_group_name` must be re-exported before each `terraform apply`. The same export is then used by `gh variable set AZ_RESOURCE_GROUP` to record that environment's RG in its GitHub environment. `terraform output -raw az_client_id` reads from the current workspace's state, so each block's `terraform output` call must follow that block's `terraform apply`.

```bash
REPO="rodekruis/qualitative-feedback-analysis"

# === Dev ===
export TF_VAR_resource_group_name=<your-dev-rg-name>
terraform workspace select dev
terraform apply

gh api repos/$REPO/environments/dev -X PUT
gh variable set AZ_CLIENT_ID       --env dev --repo $REPO --body "$(terraform output -raw az_client_id)"
gh variable set AZ_TENANT_ID       --env dev --repo $REPO --body "$TF_VAR_tenant_id"
gh variable set AZ_SUBSCRIPTION_ID --env dev --repo $REPO --body "$TF_VAR_subscription_id"
gh variable set AZ_RESOURCE_GROUP  --env dev --repo $REPO --body "$TF_VAR_resource_group_name"
gh variable set AZ_APP_NAME        --env dev --repo $REPO --body "qfa-dev-backend"
gh variable set AZ_ACR_NAME        --env dev --repo $REPO --body "$TF_VAR_acr_name"

# === Staging ===
export TF_VAR_resource_group_name=<your-staging-rg-name>
terraform workspace select staging
terraform apply

gh api repos/$REPO/environments/staging -X PUT
gh variable set AZ_CLIENT_ID       --env staging --repo $REPO --body "$(terraform output -raw az_client_id)"
gh variable set AZ_TENANT_ID       --env staging --repo $REPO --body "$TF_VAR_tenant_id"
gh variable set AZ_SUBSCRIPTION_ID --env staging --repo $REPO --body "$TF_VAR_subscription_id"
gh variable set AZ_RESOURCE_GROUP  --env staging --repo $REPO --body "$TF_VAR_resource_group_name"
gh variable set AZ_APP_NAME        --env staging --repo $REPO --body "qfa-staging-backend"
gh variable set AZ_ACR_NAME        --env staging --repo $REPO --body "$TF_VAR_acr_name"

# === Production ===
export TF_VAR_resource_group_name=<your-prd-rg-name>
terraform workspace select prd
terraform apply

gh api repos/$REPO/environments/prd -X PUT
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

If the managed identity is ever recreated (e.g. after `terraform destroy`), re-run the affected environment's block from step 6 to update `AZ_CLIENT_ID`.
