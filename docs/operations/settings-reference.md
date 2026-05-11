# Settings reference

Every environment variable the app reads. Settings are loaded by `pydantic-settings` at startup; missing required variables cause the app to fail fast.

> **Tip:** rather than editing this table by hand, you can `uv run python -c "from qfa.settings import AppSettings; import json; print(AppSettings.model_json_schema())"` to dump the live schema.

## LLM (`LLM_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `LLM_MODEL` | no | `azure_ai/mistral-medium-2505` | Routed by LiteLLM based on the prefix (`azure/…`, `azure_ai/…`, `openai/…`, …). |
| `LLM_API_KEY` | **yes** | — | Provider API key. Stored as `SecretStr`. |
| `LLM_API_BASE` | only some providers | `""` | E.g. `https://<resource>.openai.azure.com/` for Azure OpenAI. |
| `LLM_API_VERSION` | only some providers | `""` | API version where the provider expects one. |
| `LLM_TIMEOUT_SECONDS` | no | `115.0` | Per-LLM-call timeout. |
| `LLM_MAX_TOTAL_TOKENS` | no | `100000` | Token budget guard. Estimated as `len(text) / LLM_CHARS_PER_TOKEN`. |
| `LLM_CHARS_PER_TOKEN` | no | `4` | Conversion ratio used by the token budget guard. |

## Orchestrator (`ORCHESTRATOR_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ORCHESTRATOR_METADATA_FIELDS_TO_INCLUDE` | no | `[]` | JSON list. Metadata keys allowed to reach the LLM. |

## Auth (`AUTH_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AUTH_API_KEYS` | **yes** | — | JSON array of `TenantApiKey` objects. See [API key management](auth-management.md) for the shape. |

## Database (`DB_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DB_TRACK_USAGE` | no | `false` | Master switch for usage tracking. When `false`, none of the other `DB_*` variables are required. |
| `DB_URL` | only if `DB_TRACK_USAGE=true` and host/user not split | `""` | Full asyncpg URL. Used when supplied; otherwise built from the next four. |
| `DB_HOST` | only if `DB_URL` not set | `""` | |
| `DB_PORT` | no | `5432` | |
| `DB_NAME` | only if `DB_URL` not set | `""` | |
| `DB_USER` | only if `DB_URL` not set | `""` | For `entra` mode, the managed-identity principal name. |
| `DB_PASSWORD` | only when `DB_AUTH_MODE=password` | — | Stored as `SecretStr`. |
| `DB_AUTH_MODE` | no | `password` | `password` or `entra`. |
| `DB_AAD_SCOPE` | no | `https://ossrdbms-aad.database.windows.net/.default` | AAD scope for the access token (Entra mode only). |

## Logging (`LOG_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `LOG_LOGLEVEL` | no | `DEBUG` | Level for the `qfa` package (string or numeric). |
| `LOG_LOGLEVEL_3RDPARTY` | no | `WARNING` | Level for third-party libraries. |
