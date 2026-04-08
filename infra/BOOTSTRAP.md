# Infrastructure Bootstrap

This is a **one-time local setup** required before the CI/CD pipeline can manage infrastructure autonomously.

## Why is a local bootstrap needed?

The Terraform configuration in `infra/` manages Azure resources (Key Vault, App Service, managed identity, etc.). CI needs GitHub Actions environment variables to authenticate with Azure, but those variables depend on resources that Terraform creates — so the first apply must be run locally with personal credentials.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — authenticated (`az login`)
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- [GitHub CLI](https://cli.github.com/) — authenticated (`gh auth login`) with a token that has `repo` scope

The following Azure resource must already exist before running Terraform (it is referenced as a read-only data source, not created by Terraform):

- **Resource group** `qualitative-feedback-analysis-xomnia`

## Steps

### 1. Change into the infra directory

```bash
cd infra
```

### 2. Export environment variables

These are required by Terraform locally and re-used in step 6 to populate the GitHub variables.

```bash
export TF_VAR_tenant_id=<your-azure-tenant-id>
export TF_VAR_subscription_id=<your-azure-subscription-id>
export TF_VAR_resource_group_name=<your-resource-group-name>
```

### 3. Create the Terraform state backend

**NOTE:** Run this step only once. If these resources already exist, skip it.

Two resources must exist before `terraform init` can run — they are chicken-and-egg resources that live outside Terraform's management:

- **Azure Blob Storage** (`qfatfstate`) — the Terraform remote state backend
- **Container Registry** (`qfacontainerreg`) — shared ACR used as a `data` source by Terraform

```bash
bash bootstrap.sh
```

### 4. Initialize Terraform

```bash
terraform init
```

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
gh variable set AZ_ACR_NAME        --env dev --repo $REPO --body "qfacontainerreg"

# --- prd ---
gh api repos/$REPO/environments/prd -X PUT

terraform workspace select prd
gh variable set AZ_CLIENT_ID       --env prd --repo $REPO --body "$(terraform output -raw az_client_id)"
gh variable set AZ_TENANT_ID       --env prd --repo $REPO --body "$TF_VAR_tenant_id"
gh variable set AZ_SUBSCRIPTION_ID --env prd --repo $REPO --body "$TF_VAR_subscription_id"
gh variable set AZ_RESOURCE_GROUP  --env prd --repo $REPO --body "$TF_VAR_resource_group_name"
gh variable set AZ_APP_NAME        --env prd --repo $REPO --body "qfa-prd-backend"
gh variable set AZ_ACR_NAME        --env prd --repo $REPO --body "qfacontainerreg"
```

The `terraform.yaml` workflow can now run autonomously in CI.

## Subsequent infrastructure changes

After the bootstrap, infrastructure changes follow the normal workflow:

- Open a PR touching `infra/` → CI runs `terraform plan` automatically
- Merge to `main` → trigger `terraform apply` manually from the Actions tab

If the managed identity is ever recreated (e.g. after `terraform destroy`), re-run step 7 for the affected environment to update `AZ_CLIENT_ID`.
