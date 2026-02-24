# ADR-007: Separate API Schemas from Domain Models

## Status

Accepted

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
