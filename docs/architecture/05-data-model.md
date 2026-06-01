# Data model

Two surfaces: the in-memory domain models, and the on-disk usage table.

## Domain models

Domain entities live in {py:mod}`qfa.domain.models`, with usage-tracking entities split into {py:mod}`qfa.domain.usage_models`. All are Pydantic `BaseModel(frozen=True)` per [ADR-001](../adr/001-pydantic-domain-models.md). One exception is {py:class}`~qfa.domain.models.AggregateSummaryResultModel`, which is mutable so the orchestrator can attach a quality score after the judge call.

| Model | Purpose |
|---|---|
| {py:class}`~qfa.domain.models.FeedbackRecordModel` | A single beneficiary feedback record submitted by the CRM. |
| {py:class}`~qfa.domain.models.AnalysisRequestModel` / {py:class}`~qfa.domain.models.AnalysisResultModel` | Request and result for `POST /v1/analyze-bulk`. |
| {py:class}`~qfa.domain.models.SummaryRequestModel` / {py:class}`~qfa.domain.models.SummaryResultModel` / {py:class}`~qfa.domain.models.FeedbackRecordSummaryModel` | Per-record summarisation. |
| {py:class}`~qfa.domain.models.AggregateSummaryResultModel` | Single aggregate summary with judge score. |
| {py:class}`~qfa.domain.models.CodingAssignmentRequestModel` / {py:class}`~qfa.domain.models.CodingAssignmentResultModel` | Hierarchical code assignment. `coding_framework` is currently `dict[str, Any]` — a typed model exists in the API schemas but is not yet wired in. |
| `LLMResponse[T_Response]` | Generic envelope returned from {py:class}`~qfa.domain.ports.LLMPort`'s `complete` method. |
| {py:class}`~qfa.domain.models.TenantApiKey` | One row in `AUTH_API_KEYS`. |
| {py:class}`~qfa.domain.usage_models.LLMCallRecord` | One LLM call's worth of tracking data — written by {py:class}`~qfa.adapters.tracking_llm.TrackingLLMAdapter`. |
| {py:class}`~qfa.domain.usage_models.UsageMetrics`, {py:class}`~qfa.domain.usage_models.OperationStats`, {py:class}`~qfa.domain.usage_models.TenantUsageStats`, {py:class}`~qfa.domain.usage_models.TenantStats`, {py:class}`~qfa.domain.usage_models.OperationUsageStats`, {py:class}`~qfa.domain.usage_models.DistributionStats` | Aggregate views returned by `/v1/usage`, `/v1/usage/all/by-tenant`, and `/v1/usage/all/by-operation`. `TenantUsageStats` / `OperationStats` carry the tenant-top-level hierarchy with per-operation nested; `OperationUsageStats` / `TenantStats` carry the inverse operation-top-level hierarchy with per-tenant nested. Every block exposes per-invocation top-level fields plus a per-LLM-call `llm_call_stats`. `UsageMetrics` is the shared leaf class. `DistributionStats` is the shared distribution shape (avg/min/max/p5/p95/total) used for `call_duration`, `input_tokens`, and `output_tokens`. |

## Persistence — `llm_calls`

When `DB_TRACK_USAGE=true`, every LLM call appends one row to the `llm_calls` table. The schema lives in `qfa.adapters.db` and is managed by Alembic migrations under `migrations/`.

Roughly:

| Column | Meaning |
|---|---|
| `id` | UUID primary key |
| `tenant_id` | Caller, set from the authenticated `TenantApiKey` |
| `operation` | One of `analyze`, `summarize`, `summarize_aggregate`, `assign_codes` |
| `call_id` | UUID linking all LLM calls made within one API invocation — enables per-invocation cost aggregation across the fan-out from one orchestrator entry. Generated once per request by `RequestIdMiddleware` (also returned as the `X-Request-ID` header) and propagated into the `CallContext` via the route's `Depends(call_scope_for(...))`. Identical for every row produced by a single endpoint call. |
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
