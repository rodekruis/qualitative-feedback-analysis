# Operational How-Tos

Short, copy-pasteable runbooks for recurring operational tasks. Each entry is
self-contained; pick the one you need.

## Environment naming

The resource group and App Service names are **not** symmetric across
environments, so double-check both before running a command against production.

| Environment | App Service (`-n`) | Resource group (`-g`) | Key Vault |
|---|---|---|---|
| dev | `qfa-dev-backend` | `qualitative-feedback-analysis-xomnia` | `qfa-dev-keyvault` |
| staging | `qfa-staging-backend` | `qualitative-feedback-analysis-staging` | `qfa-staging-keyvault` |
| prd | `qfa-prd-backend` | `qualitative-feedback-analysis-production` | `qfa-prd-keyvault` |

Note the mismatch: the App Service name uses `prd`, but its resource group is
`…-production`; and the dev resource group is `…-xomnia`, not `…-dev`.

## Force refresh of changed Key Vault values

Secrets reach the App Service as
[Key Vault references](https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references)
(e.g. `@Microsoft.KeyVault(SecretUri=…)`) in the app settings. App Service
**caches** the resolved values, so a freshly rotated secret does not take effect
immediately: for a versionless reference the platform refetches the cache on its
own only [about every 24 hours](https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references#understand-rotation),
so a rotation can take up to a day to land unless you force it.

To force an immediate re-read, change any app setting. [Per Microsoft](https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references#understand-rotation),
"any configuration change to the app causes an app restart and an immediate
refetch of all referenced secrets" — so writing a throwaway setting does the job:

```bash
# Substitute -n / -g for your environment from the table above.
az webapp config appsettings set \
  -n qfa-prd-backend -g qualitative-feedback-analysis-production \
  --settings KV_REFRESH_TOUCH=$(date +%s)
```

`KV_REFRESH_TOUCH` is an arbitrary, unused setting; the `$(date +%s)` value just
guarantees it changes each run, so every save is a real configuration change and
therefore forces the refetch of **all** references.

Alternatives:

- **Refresh API** — forces re-resolution with no throwaway setting *and* no
  restart, by [POSTing to the `configreferences` refresh endpoint](https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references#understand-rotation):
  ```bash
  az rest --method post --url \
    "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/qualitative-feedback-analysis-production/providers/Microsoft.Web/sites/qfa-prd-backend/config/configreferences/appsettings/refresh?api-version=2022-03-01"
  ```
- **Portal**: open the App Service → **Settings → Environment variables** and
  add/save any setting. The Portal also shows a per-setting resolution status,
  which is the quickest way to confirm a reference resolved rather than silently
  keeping a stale value.

> **Don't rely on a bare restart.** `az webapp restart` (or Portal → **Restart**)
> *without* a configuration change is **not** documented to re-read Key Vault
> references, and in practice it often keeps serving the cached values. Force a
> refresh with a configuration change or the refresh API above — not a restart
> alone.

> **Tip — token staleness on the CLI.** If `az` returns `AuthorizationFailed`
> for `Microsoft.Web/sites/config/list/action` right after a role was granted
> (or a PIM role activated), your cached access token predates the grant. Run
> `az account clear && az login` to mint a fresh token, or use the Portal, which
> re-authenticates per action.

## Versionless vs. pinned secret references

If a Key Vault reference's `SecretUri` ends with a version GUID
(`…/secrets/<name>/<version>`), App Service is pinned to that exact version and
will **never** pick up a rotated secret — even after a restart or a
configuration change — until the app setting itself is repointed. Use a
**versionless** URI (no `/<version>` suffix, e.g. `…/secrets/<name>`) so
rotations are picked up by the periodic (~24 h) refresh, or immediately via a
configuration change / the refresh API above. The Terraform-managed references
in `app_service.tf` are versionless by design.

## Resize the App Service plan for an environment

The plan SKU is per environment: `dev`/`staging` on `B2`, `prd` on `P0v3` (see
[ADR-019](../adr/019-per-environment-app-service-plan-sizing.md)). Substitute
`-n` / `-g` below from the [table above](#environment-naming).

**1. Check the tier exists in that environment's region first.** This is the
step that saves an aborted apply — `P0v4`, for example, was unavailable in the
region when [ADR-019](../adr/019-per-environment-app-service-plan-sizing.md) was
written.

```bash
az group show -g qualitative-feedback-analysis-production --query location -o tsv
az appservice list-locations --sku P0V3 --linux-workers-enabled
```

> `az` wants the SKU **upper-cased** (`P0V3`) here; the azurerm provider wants
> `P0v3`. Same tier, different casing — worth twenty minutes if you miss it.

**2. Make the change.** Edit `var.app_service_plan_sku_by_env` in
`infra/variables.tf` and open a PR. CI plans **`dev` only**, so a prd-only
change shows no plan diff on the PR — that is expected. To see the real diff,
dispatch the **Terraform** workflow with `command: plan` for the target
environment, then again with `command: apply`. Applies fan out one run per
environment and are never automatic (see
[Release flow § Infrastructure changes](release-flow.md#infrastructure-changes)).

Read the plan output before applying: it must be an **in-place update (`~`)** of
`azurerm_service_plan.main`. If Terraform proposes a **replacement (`-/+`)**,
stop — replacing the plan detaches and re-attaches the web app, turning a
restart into a real outage.

**3. Expect a restart.** Scaling moves the site to new workers: the container
re-runs `python -m qfa.cli.migrate` and reloads the embedding model, so there is
a cold-start gap. With `health_check_eviction_time_in_min = 10` and a
severity-1 health-check alert, a prolonged failure pages Teams — apply in a
quiet window and watch `/v1/health`.

**4. Verify against Azure, not state.** Plan names are `qfa-<env>-plan`:

```bash
az appservice plan show -n qfa-prd-plan -g qualitative-feedback-analysis-production \
  --query "{sku:sku.name, capacity:sku.capacity, tier:sku.tier}"
```

**If the apply fails** with a scale-unit / SKU-not-supported error, that is a
real Azure constraint: an existing plan can only be scaled to a tier its scale
unit supports. The remedy is a **new** `azurerm_service_plan` in a Pv3-capable
scale unit plus repointing `azurerm_linux_web_app.backend.service_plan_id` — a
longer outage and a separate PR.

## Roll out the split CI/CD identities to an existing environment

[ADR-021](../adr/021-split-cicd-identity.md) splits the single CI/CD identity
into an infra identity (`terraform.yaml` only) and a deploy-only identity
(release/promote/build-from-commit). Environments created before that ADR
need this rollout, run **from the PR branch, before merge**, with credentials
holding `Contributor` + `Role Based Access Control Administrator` on the
environment RG and on the ACR:

```bash
export ENV=dev  # then staging, then prd
cd infra && terraform workspace select "$ENV"
terraform plan    # expect: +1 identity, +1 FIC, +2 role assignments (dev) or +1 (staging/prd), -1 acr repository writer
terraform apply
gh variable set AZ_DEPLOY_CLIENT_ID --env "$ENV" --repo rodekruis/qualitative-feedback-analysis \
  --body "$(terraform output -raw az_deploy_client_id)"
```

- **Why local:** creating and deleting role assignments needs
  `Microsoft.Authorization/roleAssignments/write|delete`, which `Contributor`
  does not have — CI cannot apply this change. A CI `terraform apply`
  attempted before the local apply fails with 403, and because
  `promote-to-staging.yaml` / `promote-to-prd.yaml` run `terraform apply` as a
  `needs:` dependency of their deploy job, that failure blocks promotion.
- **Do the `terraform apply` and the `gh variable set` back to back.** Between
  them, the release/promote workflows for that environment are broken (the
  old identity has lost ACR push; the new client ID isn't published yet).
- **First-failure diagnostics:** if `terraform apply` rejects the new
  federated credential because issuer+subject duplicates the existing one,
  the split cannot be done with a shared GitHub environment — see
  [ADR-021](../adr/021-split-cicd-identity.md)'s rejected options.
- **Verify after each environment:** `Build from commit` with
  `deploy_to_dev: true` for dev (exercises push + `webapp config container
  set` + `webapp update --set tags.*` on the new identity); for
  staging/prd, a `Promote to <env>` of the currently-deployed tag (a no-op
  redeploy that still exercises `terraform apply` and both `az webapp`
  calls). This is the only way `Website Contributor`'s sufficiency gets
  proven — nothing in `make test` or CI can check Azure RBAC.
- **Rollback:** revert the four workflow lines to `vars.AZ_CLIENT_ID` and
  re-apply the previous `cicd.tf` locally (restores
  `github_acr_repository_writer`). The deploy identity can be left in place;
  it is inert once no workflow uses it.
