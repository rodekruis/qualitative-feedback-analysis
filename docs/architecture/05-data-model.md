# Data model

Two surfaces: the in-memory domain models, and the on-disk usage table.

## Domain models

All domain entities live in `qfa.domain.models` and are Pydantic `BaseModel(frozen=True)` per [ADR-001](../adr/001-pydantic-domain-models.md). One exception is `AggregateSummaryResultModel`, which is mutable so the orchestrator can attach a quality score after the judge call.

| Model | Purpose |
|---|---|
| `FeedbackRecordModel` | A single beneficiary feedback record submitted by the CRM. Carries `id`, `text` / `content`, optional metadata. |
| `AnalysisRequestModel` / `AnalysisResultModel` | Request and result for `POST /v1/analyze`. |
| `SummarizeRequestModel` / `SummaryResultModel` / `FeedbackRecordSummaryModel` | Per-record summarisation. |
| `AggregateSummarizationRequestModel` / `AggregateSummaryResultModel` | Single aggregate summary with judge score. |
| `CodingAssignmentRequestModel` / `CodingAssignmentResultModel` | Hierarchical code assignment. `coding_framework` is currently `dict[str, Any]` — a typed model exists in the API schemas but is not yet wired in. |
| `LLMResponse[T_Response]` | Generic envelope returned from `LLMPort.complete`. Carries the structured payload plus `model`, `prompt_tokens`, `completion_tokens`, `cost`. |
| `TenantApiKey` | One row in `AUTH_API_KEYS`. Fields: `key_id`, `key` (`SecretStr`), `tenant_id`, optional `is_superuser`. |
| `LLMCallRecord` | One LLM call's worth of tracking data — written by `TrackingLLMAdapter`. |
| `UsageStats`, `DistributionStats`, `TokenStats` | Aggregate views returned by `/v1/usage`. |

## Persistence — `llm_calls`

When `DB_TRACK_USAGE=true`, every LLM call appends one row to the `llm_calls` table. The schema lives in `qfa.adapters.db` and is managed by Alembic migrations under `migrations/`.

Roughly:

| Column | Meaning |
|---|---|
| `id` | UUID primary key |
| `tenant_id` | Caller, set from the authenticated `TenantApiKey` |
| `operation` | One of `analyze`, `summarize`, `summarize_aggregate`, `assign_codes` |
| `model` | The LiteLLM model string used |
| `prompt_tokens`, `completion_tokens` | From the provider response |
| `cost` | Computed from LiteLLM's cost map; zero when the model has no published pricing |
| `latency_seconds` | Wall-clock duration of the call |
| `error_class` | Exception class name if the call failed |
| `created_at` | UTC timestamp |

## Migrations

Run by `python -m qfa.cli.migrate` (entry point in `qfa.cli.migrate`). Invoked from `entrypoint.sh` before `uvicorn` starts. Uses Alembic with a Postgres advisory lock so concurrent replicas wait for one migrator to finish — see [Deployment: runtime overview](../operations/deployment.md) for the operational story.

## What lives outside the domain

- **API schemas** (`qfa.api.schemas`, `qfa.api.schemas_usage`) — Pydantic models for HTTP request/response shapes. Per [ADR-007](../adr/007-separate-api-schemas.md), API schemas are separated from domain models only when fields differ, shapes are reshaped, or HTTP-only fields are added.
- **Tracking-table rows** — `qfa.adapters.db` defines the SQLAlchemy table mapping; the domain only sees `LLMCallRecord`.
