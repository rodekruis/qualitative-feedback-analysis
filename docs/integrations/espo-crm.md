# EspoCRM integration

EspoCRM is the primary upstream feeding feedback records into the service. The integration is one-way: EspoCRM calls the qfa backend's HTTP endpoints; the backend does not call EspoCRM.

## What the scripts do

Server-side EspoCRM scripts in `scripts/espo_crm/` compose request bodies and call:

| Script | Backend endpoint |
|---|---|
| `set_analyze_*` | `POST /v1/analyze-bulk` |
| `set_summarize_*` | `POST /v1/summarize` |
| `set_summarize_aggregate_*` | `POST /v1/summarize-bulk` |
| `set_assign_codes_*` | `POST /v1/assign-codes` |

Each script reads the relevant EspoCRM fields, builds the JSON body, sends it with an `Authorization: Bearer <key>` header, and writes the response back to a target EspoCRM field.

## Display output

The `/v1/summarize-bulk` response includes a backend-rendered `pretty_output`
field — a human-readable text block (quality dots, title, summary) ready to
write straight into an EspoCRM field. The formatting lives entirely in the
backend, so the scripts do not assemble it.

Its `QUALITY`/`TITLE`/`SUMMARY` headers are localized to the request's
`output_language` (the same field that drives the title/summary language).
Supported languages are English, French, Spanish, Arabic, Russian, Dutch, and
Ukrainian; any other or absent value falls back to English headers. The
technical `IDs` label is not localized.

## Authentication

EspoCRM stores the bearer token as a server-side secret. Provisioning and rotation use the standard flow in [API key management](../operations/auth-management.md).

## Field-name expectations

The scripts must use the field names that the backend currently exposes (the `feedback_records` / `content` naming, per the recent ubiquitous-language migration). When the backend's API field names change, the EspoCRM scripts must be updated in the same release — there is no Pydantic-alias compatibility layer.

## Empty descriptions

A feedback record whose description is blank is sent through as an empty `content` string. The backend tolerates this: empty records are dropped from bulk requests, and per-record endpoints return a 200 empty result rather than rejecting the call.
