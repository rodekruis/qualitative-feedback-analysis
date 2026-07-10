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
