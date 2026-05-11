# EspoCRM integration

EspoCRM is the primary upstream feeding feedback records into the service. The integration is one-way: EspoCRM calls the qfa backend's HTTP endpoints; the backend does not call EspoCRM.

## What the scripts do

Server-side EspoCRM scripts in `scripts/espo_crm/` compose request bodies and call:

| Script | Backend endpoint |
|---|---|
| `set_analyze_*` | `POST /v1/analyze` |
| `set_summarize_*` | `POST /v1/summarize` |
| `set_summarize_aggregate_*` | `POST /v1/summarize-aggregate` |
| `set_assign_codes_*` | `POST /v1/assign_codes` |

Each script reads the relevant EspoCRM fields, builds the JSON body, sends it with an `Authorization: Bearer <key>` header, and writes the response back to a target EspoCRM field.

## Authentication

EspoCRM stores the bearer token as a server-side secret. Provisioning and rotation use the standard flow in [API key management](../operations/auth-management.md).

## Field-name expectations

The scripts must use the field names that the backend currently exposes (the `feedback_records` / `content` naming, per the recent ubiquitous-language migration). When the backend's API field names change, the EspoCRM scripts must be updated in the same release — there is no Pydantic-alias compatibility layer.
