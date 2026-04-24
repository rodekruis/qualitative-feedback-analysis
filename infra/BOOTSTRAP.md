# Infrastructure Bootstrap

This is a **one-time local setup** required before the CI/CD pipeline can manage infrastructure autonomously.

## Why is a local bootstrap needed?

The Terraform configuration in `infra/` manages Azure resources (Key Vault, App Service, managed identity, etc.). CI needs GitHub Actions environment variables to authenticate with Azure, but those variables depend on resources that Terraform creates — so the first apply must be run locally with personal credentials.

Two of the resources Terraform depends on cannot be managed by Terraform itself: the storage account holding Terraform's own state, and the container registry that Terraform reads as a data source. These are the chicken-and-egg dependencies that `bootstrap.sh` creates before `terraform init` can run.

Furthermore, the GitHub Actions require a managed identity and a federated identity credential
to authenticate. These are managed by Terraform.
To initially create them, we need to run `terraform apply`
locally with personal credentials. Subsequently, `terraform apply` can and will be run
via GitHub Actions.


## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — authenticated (`az login`)
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- [GitHub CLI](https://cli.github.com/) — authenticated (`gh auth login`) with a token that has `repo` scope
- **Azure resource groups** that already exist. You will need:
  - One **per environment** (dev, staging, prd) for that environment's Terraform-managed resources (App Service, Key Vault, managed identity, etc.). Their names are passed to Terraform via `TF_VAR_resource_group_name` in step 6, one at a time.
  - One for the **Terraform state storage account** — passed via `TF_VAR_tf_state_resource_group_name` in step 2. May be the same as one of the env RGs or its own.
  - One for the **shared container registry** — passed via `TF_VAR_acr_resource_group_name` in step 2. May be the same as one of the env RGs or its own.

  For a minimal single-RG deployment, all five roles can point at the same RG. For multi-RG isolation, give each role its own RG. Create each via the Azure portal or `az group create -n <name> -l westeurope`.

### Required roles to run initial terraform apply:

* Contributor
* Key Vault Administrator
* Role Based Access Control Administrator

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
# Both are read by bootstrap.sh, by the `terraform init` command in step 4
# (for tf_state_resource_group_name — which cannot be a Terraform backend
# variable because backend blocks forbid variable interpolation), and by
# Terraform itself (cicd.tf looks up the state SA to grant CI a blob role;
# container_registry.tf looks up the ACR).
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

### 4. Grant yourself data-plane access to the Terraform state storage account

The backend uses AAD authentication (`use_azuread_auth = true` in `providers.tf`), which talks to blob storage as you rather than fetching the SA's shared key. Azure Contributor/Owner on the resource group does **not** grant data-plane blob operations — you need `Storage Blob Data Contributor` scoped to the SA itself. Without it, `terraform init` in the next step will fail with a 403 `AuthorizationPermissionMismatch`.

```bash
SA_ID=$(az storage account show \
  --name "$TF_VAR_tf_state_storage_account" \
  --resource-group "$TF_VAR_tf_state_resource_group_name" \
  --query id -o tsv)

az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --scope "$SA_ID"
```

Role assignments take 30–60 seconds to propagate in Azure AD. If the next step still 403s, wait a minute and retry.

### 5. Initialize Terraform

Terraform's `backend` block does not allow variable interpolation, so the resource group and storage account names must be supplied at `terraform init` time via `-backend-config` flags:

```bash
terraform init \
  -backend-config="resource_group_name=$TF_VAR_tf_state_resource_group_name" \
  -backend-config="storage_account_name=$TF_VAR_tf_state_storage_account"
```


> [!NOTE]
> If `terraform init` fails with an Azure CLI authorizer or tenant ID error, make sure you are successfully logged in to Azure:
> 
> ```bash
> az login
> ```
> 
> Then re-run the `terraform init` command above.
> If it still fails, make sure your roles are activated in the Azure portal.

### 6. Set the repo-scoped GitHub Actions variables

These GitHub variables are shared across all environments and only need to be set once per deployment. They are read by the `terraform.yaml` workflow so CI can locate the Terraform state backend and the container registry without hardcoding their names.

```bash
REPO="rodekruis/qualitative-feedback-analysis"

echo "$TF_VAR_tenant_id"                    | gh variable set AZ_TENANT_ID                --repo "$REPO"
echo "$TF_VAR_subscription_id"              | gh variable set AZ_SUBSCRIPTION_ID          --repo "$REPO"
echo "$TF_VAR_tf_state_resource_group_name" | gh variable set AZ_TF_STATE_RESOURCE_GROUP  --repo "$REPO"
echo "$TF_VAR_tf_state_storage_account"     | gh variable set AZ_TF_STATE_STORAGE_ACCOUNT --repo "$REPO"
echo "$TF_VAR_acr_resource_group_name"      | gh variable set AZ_ACR_RESOURCE_GROUP       --repo "$REPO"
echo "$TF_VAR_acr_name"                     | gh variable set AZ_ACR_NAME                 --repo "$REPO"
```

## Next: create each environment

> [!NOTE]
> The steps above create the shared Terraform backend and container registry, and the repo-scoped GitHub variables. They do **not** yet create any App Service, Key Vault, or managed identity — those are per-environment and are provisioned in the next document.

Run [setup-new-env.md](setup-new-env.md) once for each environment (`dev`, `staging`, `prd`). That doc creates a Terraform workspace, applies the per-environment resources (including the managed identity + federated credential that lets GitHub Actions authenticate without any secrets), configures the per-environment GitHub variables, and seeds Key Vault secrets.

## Subsequent infrastructure changes

After the bootstrap and per-environment setup are complete, infrastructure changes follow the normal workflow:

- Open a PR touching `infra/` → CI runs `terraform plan` automatically
- Merge to `main` → trigger `terraform apply` manually from the Actions tab

If the managed identity is ever recreated (e.g. after `terraform destroy`), re-run steps 4 and 5 of [setup-new-env.md](setup-new-env.md) for the affected environment to update `AZ_CLIENT_ID`.
