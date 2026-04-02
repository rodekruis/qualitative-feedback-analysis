# Infrastructure Bootstrap

This is a **one-time local setup** required before the CI/CD pipeline can manage infrastructure autonomously.

## Why is a local bootstrap needed?

The Terraform configuration in `infra/` manages both Azure resources *and* the GitHub Actions environment variables that CI uses to authenticate with Azure (`AZ_CLIENT_ID`, `AZ_TENANT_ID`, etc.).

This creates a chicken-and-egg problem: CI needs those variables to run Terraform, but Terraform is what creates them. The solution is to run Terraform once locally — using your personal credentials — to bootstrap the GitHub environment. After that, CI can take over for all subsequent `plan` and `apply` runs.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — authenticated (`az login`)
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- [GitHub CLI](https://cli.github.com/) — authenticated (`gh auth login`) with a token that has `repo` scope (needed to create GitHub environments and variables)

## Steps

### 1. Create the Terraform state backend

The state backend (Azure Blob Storage) must exist before `terraform init` can run. This is itself a chicken-and-egg resource — it lives outside Terraform's management.

```bash
cd infra
bash bootstrap.sh
```

This only needs to be run once ever. If the storage account already exists, skip this step.

### 2. Initialize Terraform

```bash
terraform init
```

### 3. Create workspaces

Terraform uses workspaces to manage `dev` and `prd` environments with separate state files.

```bash
terraform workspace new dev
terraform workspace new prd
```

### 4. Apply for each environment

Run `terraform apply` once per workspace. This creates all Azure resources (Key Vault, App Service, managed identity) *and* the GitHub environment variables that CI will use going forward.

```bash
# Dev environment
terraform workspace select dev
terraform apply

# Production environment
terraform workspace select prd
terraform apply
```

The `GITHUB_TOKEN` environment variable must be set for the GitHub provider:

```bash
export GITHUB_TOKEN=$(gh auth token)
terraform apply
```

After both applies complete, the GitHub environments (`dev`, `prd`) and their variables are live. The `terraform.yaml` workflow can now run autonomously in CI.

## Subsequent infrastructure changes

After the bootstrap, infrastructure changes follow the normal workflow:

- Open a PR touching `infra/` → CI runs `terraform plan` automatically
- Merge to `main` → trigger `terraform apply` manually from the Actions tab
