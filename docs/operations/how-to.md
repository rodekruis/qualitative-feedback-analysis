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
immediately — the platform refreshes the cache periodically (within a few
minutes to ~24h), and always re-reads every reference on startup.

To force an immediate re-read, recycle the app. Any app-setting change triggers
a worker restart, so writing a throwaway setting does the job:

```bash
# Substitute -n / -g for your environment from the table above.
az webapp config appsettings set \
  -n qfa-prd-backend -g qualitative-feedback-analysis-production \
  --settings KV_REFRESH_TOUCH=$(date +%s)
```

`KV_REFRESH_TOUCH` is an arbitrary, unused setting; the `$(date +%s)` value just
guarantees it changes each run so the save always recycles the app. The app
re-resolves **all** Key Vault references on the restart that follows.

Equivalent alternatives:

- **Restart only** (leaves no throwaway setting behind):
  ```bash
  az webapp restart -n qfa-prd-backend -g qualitative-feedback-analysis-production
  ```
- **Portal**: open the App Service → **Restart**, or add/save any setting under
  **Settings → Environment variables**. The Portal also shows a per-setting
  resolution status, which is the quickest way to confirm a reference resolved
  rather than silently keeping a stale value.

> **Tip — token staleness on the CLI.** If `az` returns `AuthorizationFailed`
> for `Microsoft.Web/sites/config/list/action` right after a role was granted
> (or a PIM role activated), your cached access token predates the grant. Run
> `az account clear && az login` to mint a fresh token, or use the Portal, which
> re-authenticates per action.

## Versionless vs. pinned secret references

If a Key Vault reference's `SecretUri` ends with a version GUID
(`…/secrets/<name>/<version>`), App Service is pinned to that exact version and
will **never** pick up a rotated secret — even after a restart — until the app
setting itself is changed. Use a **versionless** URI (trailing `/`, no version)
so rotations are picked up by the periodic refresh or a recycle. The
Terraform-managed references in `app_service.tf` are versionless by design.
