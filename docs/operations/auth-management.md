# Auth and API key management

The service authenticates callers with simple bearer-token API keys, scoped to a tenant. This page covers operating that system.

For the design ({py:class}`~qfa.domain.models.TenantApiKey`, `validate_api_key`, middleware), see [Components](../architecture/03-components.md) and [ADR-005](../adr/005-bearer-auth.md).

## How auth works at runtime

1. Caller sends `Authorization: Bearer <key>`.
2. The middleware compares `<key>` against every entry in `AUTH_API_KEYS` using `secrets.compare_digest` (constant-time).
3. On match, the request is tagged with the matching `tenant_id` (and `is_superuser` flag). On no match → 401.

`GET /v1/health` skips auth. Everything else requires it.

## API key shape

`AUTH_API_KEYS` is a JSON array of objects used for environment-based (startup) keys. Five fields:

| Field | Type | Notes |
|---|---|---|
| `key_id` | string | Unique identifier for this key. Used for logging and rotation; not for auth. |
| `name` | string | Human-readable label (e.g. `"crm-production"`). |
| `key` | string | The secret. Stored as `SecretStr` after load; never logged. |
| `tenant_id` | string | The tenant this key represents. |
| `is_superuser` | bool, optional | Default `false`. Grants access to `/v1/usage/all`. |

Example:

```json
[
    {
        "key_id": "k-prd-0",
        "name": "crm-production",
        "key": "sk-prod-abc123def456",
        "tenant_id": "tenant-redcross-nl",
        "is_superuser": false
    }
]
```

## Creating keys via the API

Keys can be created at runtime through `POST /v1/auth/keys` (requires a superuser key). The server generates both the key value and its unique identifier — you do **not** supply them:

```http
POST /v1/auth/keys HTTP/1.1
Authorization: Bearer <superuser-key>
Content-Type: application/json

{
    "key_name": "crm-production",
    "tenant_id": "tenant-redcross-nl",
    "is_superuser": false
}
```

Response:

```json
{
    "key_id": "a1b2c3d4-...",
    "api_key": "Tx3k8..."
}
```

The `api_key` value is the plain secret. **It is shown only once** — copy it and share it with the tenant immediately. It is hashed before storage and cannot be retrieved again. The `key_id` is used for logging and deletion.

## Production: rotating and adding keys

In production the JSON is stored as the `auth-api-keys` secret in Key Vault and loaded via Key Vault references. Use the helper script — never edit the secret by hand:

```bash
az login
export AZURE_KEYVAULT="qfa-${ENV}-keyvault"   # e.g. qfa-prd-keyvault

# Add a key for a tenant (keeps existing keys)
uv run python3 scripts/update_auth_api_keys.py --add <tenant>

# Replace all keys for a tenant with one new key
uv run python3 scripts/update_auth_api_keys.py --replace <tenant>

# Remove all keys for a tenant
uv run python3 scripts/update_auth_api_keys.py --remove <tenant>
```

The script prints the generated key to stdout. **Copy it and share it with the tenant immediately** — Key Vault stores it, but you cannot retrieve it again from the CLI in clear text.

Key rotation steps:

1. `--add <tenant>` — adds a new key for the tenant alongside the old one.
2. Give the new key to the tenant.
3. Wait until you've verified the tenant is using the new key (look for the new `key_id` in logs).
4. `--replace <tenant>` with their now-confirmed new key, or `--remove <tenant>` plus `--add` if you want a clean cut.

App Service picks up Key Vault changes within a few minutes. To force an immediate refresh, see [Operational how-tos § Force refresh of changed Key Vault values](how-to.md#force-refresh-of-changed-key-vault-values).

## Local dev

For local development, set `AUTH_API_KEYS` directly in `.env` or your shell:

```bash
export AUTH_API_KEYS='[{"key_id":"local-0","name":"local","key":"dev-key","tenant_id":"local","is_superuser":true}]'
```

`is_superuser=true` is convenient locally because `/v1/usage/all` then works.

## Superuser scope

`is_superuser=true` only grants access to the cross-tenant `GET /v1/usage/all` route. It does **not** elevate access to other tenants' analyze/summarize/assign-codes endpoints — those are scoped purely by `tenant_id` derived from the matching key.

## What's not supported (yet)

- Per-key revocation by `key_id` alone (rotation goes through the script's `--replace` / `--remove` per-tenant flow).
- Per-key rate limits.
- Non-bearer auth schemes (OAuth, mTLS).

These are deliberate omissions for v1, not bugs.
