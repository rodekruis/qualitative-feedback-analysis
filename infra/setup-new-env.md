# Set Up a New Environment

Run this once per environment (`dev`, `staging`, `prd`, or any additional environment added later). It assumes [BOOTSTRAP.md](BOOTSTRAP.md) has already been completed for this deployment.

Each environment lives in its own Azure resource group and its own Terraform workspace, so state and resources stay isolated between environments.

## Prerequisites

- [BOOTSTRAP.md](BOOTSTRAP.md) has been completed for this deployment.
- The shared environment variables from [BOOTSTRAP.md § Export environment variables](BOOTSTRAP.md#2-export-environment-variables) are exported in your current shell (re-export them if you opened a new shell).
- An Azure resource group exists for this environment. Create one via the Azure portal or `az group create -n <name> -l westeurope`.
- The roles listed in [BOOTSTRAP.md § Required roles](BOOTSTRAP.md#required-roles-to-run-initial-terraform-apply) on the environment's resource group and on its Key Vault.

## Steps

### 1. Change into the infra directory

```bash
cd infra
```

### 2. Export the environment's resource group

`TF_VAR_resource_group_name` is the only per-environment Terraform variable. Re-export it each time you switch environments.
PostgreSQL Entra admin is configured automatically from the App Service system-assigned managed identity.

```bash
export ENV=dev  # or staging, prd, ...
export TF_VAR_resource_group_name=<your-env-rg-name>
```

### 3. Create and select the Terraform workspace

Terraform uses [workspaces](https://developer.hashicorp.com/terraform/language/state/workspaces) to keep per-environment state files separate.

```bash
terraform workspace new "$ENV"
# If the workspace already exists, select it instead:
# terraform workspace select "$ENV"
```

### 4. Apply

```bash
terraform apply
```

This provisions the App Service, Key Vault, managed identity, and federated identity credential for this environment.
This manual step is especially required because GitHub Actions need the federated identity credential
to modify the resource group in subsequent CI/CD runs. This is a chicken-and-egg problem
-- without this manual run CI/CD does not have access to Azure.

### 5. Configure the environment's GitHub variables

`terraform output -raw az_client_id` reads from the current workspace's state, so this step must follow the `terraform apply` above.

```bash
REPO="rodekruis/qualitative-feedback-analysis"

gh api repos/$REPO/environments/$ENV -X PUT
gh variable set AZ_CLIENT_ID       --env "$ENV" --repo "$REPO" --body "$(terraform output -raw az_client_id)"
gh variable set AZ_RESOURCE_GROUP  --env "$ENV" --repo "$REPO" --body "$TF_VAR_resource_group_name"
gh variable set AZ_APP_NAME        --env "$ENV" --repo "$REPO" --body "qfa-${ENV}-backend"
```

After this, the `terraform.yaml` workflow can manage this environment's infrastructure autonomously in CI.

### 6. Seed Key Vault secrets

The App Service reads three secrets from Key Vault at runtime via [Key Vault references](https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references) (configured in `app_service.tf`). Terraform creates the vault and grants the App Service read access (`Key Vault Secrets User`), but does **not** manage secret values — those are set out-of-band to keep them out of Terraform state.

The Key Vault uses RBAC authorization, so Azure Contributor/Owner on the resource group alone does **not** grant data-plane access to secrets. You must first assign yourself `Key Vault Secrets Officer` on the vault.

```bash
# Grant yourself write access to this environment's secrets
VAULT_ID=$(az keyvault show --name "qfa-${ENV}-keyvault" --query id -o tsv)
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --scope "$VAULT_ID"

# Set the two LLM secrets
az keyvault secret set --vault-name "qfa-${ENV}-keyvault" --name "llm-api-base" --value "<your-azure-openai-endpoint-url>"
az keyvault secret set --vault-name "qfa-${ENV}-keyvault" --name "llm-api-key"  --value "<your-llm-api-key>"
```

For the `auth-api-keys` secret, prefer [`scripts/update_auth_api_keys.py`](../scripts/update_auth_api_keys.py) — it generates a secure token, manages the JSON shape, and works for the initial seed (the secret does not need to exist yet). See the module docstring at the top of the script for the full set of operations (`--add`, `--replace`, `--remove`).

```bash
export AZURE_KEYVAULT="qfa-${ENV}-keyvault"
uv run python3 scripts/update_auth_api_keys.py --add <tenant>
```

**Secrets overview**:

| Secret | Description |
|--------|-------------|
| `llm-api-base`   | Base URL of your Azure OpenAI deployment (e.g. `https://<resource>.openai.azure.com/`) |
| `llm-api-key`    | API key for the Azure OpenAI deployment |
| `auth-api-keys`  | JSON array of API-key objects that authenticate callers to this backend (see [README § API Keys](../README.md#api-keys)) |

Without these secrets the App Service will start and pass health checks, but API calls will fail with a Key Vault reference resolution error.

## Re-running after `terraform destroy`

If the managed identity is ever recreated (e.g. after `terraform destroy`), re-run steps 4 and 5 for the affected environment to update `AZ_CLIENT_ID` in its GitHub environment.
