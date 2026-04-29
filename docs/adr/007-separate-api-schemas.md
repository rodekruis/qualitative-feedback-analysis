# ADR-007: Separate API Schemas from Domain Models

## Status

Accepted (amended 2026-04-29 — see "Scoping clarification" below)

## Context

The project uses Pydantic for both domain models (`domain/models.py`) and
API request/response definitions. The question is whether to reuse domain
models directly as API schemas or maintain separate schema classes in the
API layer.

Currently the domain model `AnalysisResult` contains fields like `model`,
`prompt_tokens`, and `completion_tokens` that are internal to the LLM
interaction. The API response `AnalyzeResponse` contains `analysis`,
`document_count`, and `request_id` — a different shape entirely.

## Decision

Maintain separate Pydantic models in `api/schemas.py` for the API contract.
Domain models in `domain/models.py` serve the internal layers.

## Options Considered

### Option A: Reuse domain models as API schemas (rejected)

- **Pro**: Zero duplication. Fewer files. Changes to the domain model
  automatically update the API contract.
- **Con**: The "automatic update" is the problem. A domain model refactor
  (renaming a field, adding an internal field) silently changes the public
  API. The API contract should be an explicit, deliberate choice — not a
  side effect of internal refactoring. Additionally, the domain
  `AnalysisResult` contains LLM metadata (`prompt_tokens`,
  `completion_tokens`, `model`) that should not be exposed to API consumers
  by default (it leaks internal implementation details and creates coupling
  if the LLM provider changes).

### Option B: Separate API schemas (chosen)

- **Pro**: The API contract is explicitly defined and can evolve
  independently. Internal fields (`prompt_tokens`, `completion_tokens`) are
  not exposed. The domain model can be refactored without risking a breaking
  API change. Data minimization: the API response only contains what the
  consumer needs (`analysis`, `document_count`, `request_id`).
- **Con**: Two sets of Pydantic models with some field overlap. A thin
  mapping layer in the route handler converts between them.
- **Mitigation**: The mapping is a few lines in the route handler, not a
  separate translation module. The cost is trivial compared to the safety
  benefit.

## Consequences

- `api/schemas.py` defines `AnalyzeRequest`, `AnalyzeResponse`,
  `HealthResponse`, `ErrorResponse`, and supporting models.
- `domain/models.py` defines `FeedbackDocument`, `AnalysisRequest`,
  `AnalysisResult`, `LLMResponse`, `TenantApiKey`.
- Route handlers map between the two:
  - Inbound: `AnalyzeRequest` (API) → `AnalysisRequest` (domain)
  - Outbound: `AnalysisResult` (domain) → `AnalyzeResponse` (API)
- Adding a field to the API response is a conscious choice in `schemas.py`,
  not an accidental side effect of a domain change.
- OpenAPI auto-generated documentation reflects the API schemas, not the
  domain models. Schema names in Swagger/Redoc are consumer-friendly
  (`AnalyzeRequest`, `AnalyzeResponse`), not domain-internal.

## Participants

- Architect (proposed separate schemas)
- UX advocate (strongly supported — API contract must not leak internals)
- Domain expert (supported — data minimization requirement)
- Devil's advocate (proposed reuse, accepted separation for v1 given the
  shape divergence between domain and API models)

## Scoping clarification (2026-04-29)

After observing this ADR being applied uniformly to *every* API response
— including the usage-tracking endpoints, where the domain aggregates
(`UsageStats`, `OperationStats`, `DistributionStats`, `TokenStats`) are
already the correct external shape with no internal fields to hide —
the rule is being narrowed to the cases where its justification actually
applies.

The two arguments above hold for response models that:

- **Hide internal fields** (e.g. `AnalyzeResponse` strips
  `prompt_tokens`, `completion_tokens`, `model`, `cost` from
  `AnalysisResult`), or
- **Reshape the wire format** (e.g. `AnalyzeResponse` exposes
  `document_count` and `request_id`, neither of which exists on the
  domain object).

They do **not** hold for response models that mirror the domain object
field-for-field with no transformation. In that case the duplication
is pure overhead: every domain field has to be repeated, conversion
mappers (`_to_usage_response` etc.) sit between them adding nothing,
and the "API can evolve independently" benefit is hypothetical because
the two models will track each other in lockstep anyway.

### Updated rule

- **Default to returning the domain type directly** (FastAPI accepts
  any Pydantic model as `response_model`).
- Add a separate API model only when at least one of these is true:
  1. The response hides one or more internal fields.
  2. The response adds HTTP-layer fields (e.g. echoed query params,
     pagination cursors, `request_id`) that don't belong on the domain
     object.
  3. The wire format needs different field names, types, or nesting
     than the domain shape.
- When (2) is the only reason, prefer a **thin subclass** of the
  domain type that adds the HTTP-only fields, rather than a full
  parallel mirror with conversion mappers.
- Request models remain separate by default — they encode HTTP-layer
  concerns (Swagger examples, validation rules, optional
  HTTP-controlled flags) that don't belong on domain models.

### What changed in the codebase under this clarification

- `DistributionStatsResponse`, `TokenStatsResponse`,
  `OperationStatsResponse` — pure mirrors, deleted.
- `UsageStatsResponse` — was a pure mirror plus two echo fields
  (`from`, `to`); now a thin subclass of `UsageStats` adding only
  those two fields.
- `AllUsageStatsResponse` — was composed of mirror types; now
  composed of domain `UsageStats` directly, plus the same echo fields.
- `Decimal → float` JSON serialization moved from the response models
  onto `UsageStats.total_cost_usd` and `OperationStats.cost_usd` — JSON
  serialization is a generic concern of any external boundary, not
  HTTP-specific.

`AnalyzeResponse`, `SummarizeResponse`, `SummarizeAggregateResponse`,
`AssignCodesResponse`, `FeedbackItemSummary`, `AggregateSummary` are
unaffected — they hide internal LLM metadata, so the original ADR
still applies.
