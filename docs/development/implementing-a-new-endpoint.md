# Implementing a new endpoint

A walk-through of adding a new HTTP endpoint to the service, in the order a
change actually flows through the hexagonal layers: domain first, then the
application service, then the API boundary, and finally the cross-cutting
concerns (authentication and usage tracking) that every endpoint inherits.

This is the *how*. For the *why* behind the structure — the layer rules, the
ports-and-adapters split, the single-orchestrator decision — read the
[architecture overview](../architecture/index.md) first. The
[ubiquitous language](../ubiquitous_language.md) governs the names a new
endpoint introduces; check it before inventing a term.

The running example below adds a hypothetical `POST /v1/classify` endpoint
that runs one LLM call over a single feedback record. Substitute your own
operation throughout.

## The shape of a request

Every inference endpoint follows the same path. A driving adapter (the FastAPI
route) maps the HTTP request into a domain request, hands it to the
`Orchestrator`, and maps the domain result back into an HTTP response. The
orchestrator anonymises the text, calls the LLM through the `LLMPort`, and
de-anonymises the result. Nothing in the inner layers knows it is being driven
over HTTP.

```{mermaid}
sequenceDiagram
    participant C as Caller
    participant R as Route (qfa.api.routes)
    participant O as Orchestrator (qfa.services)
    participant L as LLMPort (qfa.domain.ports)
    C->>R: POST /v1/classify (+ Bearer key)
    R->>R: authenticate, map API → domain
    R->>O: classify(domain_request, deadline)
    O->>L: complete(anonymised prompt, tenant_id)
    L-->>O: structured result
    O-->>R: domain result
    R-->>C: API response (+ X-Request-ID)
```

The [components page](../architecture/03-components.md) describes each
participant in full; this page covers the edits needed to add one more.

## 1. Fix the contract

Settle three things before writing code:

- The method, path, and tag. Inference endpoints are versioned under `/v1/`
  and grouped in the OpenAPI docs by an `Inference` tag.
- The request and response fields, and their validation bounds.
- Which **operation** the endpoint represents. Operations are the unit of
  usage accounting (see step 6); an endpoint maps to exactly one.

Per-record endpoints (`/v1/summarize`, `/v1/assign-codes`) take a single
`feedback_record` and return one result; bulk endpoints (`/v1/analyze-bulk`)
take a list and return aggregated output. Follow whichever convention matches
the new endpoint so the request shape is predictable.

## 2. Add the domain models

Domain request and result models live in `qfa.domain.models`. They are frozen
Pydantic models — immutable value objects validated at the domain boundary, as
decided in
[ADR-001: Use Pydantic for Domain Models](../adr/001-pydantic-domain-models.md).
They carry only what the use case needs, and they are independent of the HTTP
schema.

```python
from pydantic import BaseModel, ConfigDict, Field


class ClassificationRequestModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    feedback_record: FeedbackRecordModel
    labels: tuple[str, ...] = Field(min_length=1)
    tenant_id: str


class ClassificationResultModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    confidence: float | None
```

The `tenant_id` is part of the domain request but is **never** supplied by the
caller — the route injects it from the authenticated key (step 5). Treating it
as a request field rather than ambient state keeps the use case pure and
testable.

## 3. Implement the use case on the orchestrator

The application layer is a single `Orchestrator` class in
`qfa.services.orchestrator`. New use cases are added as **methods** on that one
class, not as new orchestrator implementations and not behind a new driving
port — see
[ADR-011: Drop Swappable-Orchestrator Requirement](../adr/011-drop-orchestrator-port.md)
and the [orchestrator section](../architecture/03-components.md#the-orchestrator)
of the components page. Per-task behaviour is selected by the route calling the
appropriate method.

A use-case method takes the domain request and an absolute `deadline`, and
returns a domain result. The established shape — visible on the existing
`summarize`, `analyze`, and `assign_codes` methods — is:

1. Derive a per-call timeout from the deadline, so a slow endpoint cannot run
   past the request budget.
2. Anonymise the record text before it reaches the model.
3. Call the model through the `LLMPort`, passing `tenant_id` and the Pydantic
   `response_model` the provider must return.
4. De-anonymise the response and validate it into the domain result.

```python
async def classify(
    self,
    request: ClassificationRequestModel,
    deadline: datetime,
) -> ClassificationResultModel:
    """Assign one label to a feedback record via a single LLM call."""
    timeout = self._check_deadline_and_get_timeout(deadline)
    anonymised, mapping = self._anonymizer.anonymize(
        str(request.feedback_record)
    )
    completion = await self._llm.complete(
        system_message=_CLASSIFY_PROMPT,
        user_message=anonymised,
        tenant_id=request.tenant_id,
        response_model=ClassificationResultModel,
        timeout=timeout,
    )
    restored = self._anonymizer.deanonymize(
        completion.structured.model_dump_json(), mapping
    )
    return ClassificationResultModel.model_validate_json(restored)
```

The orchestrator depends only on ports — `LLMPort`, `AnonymizationPort`,
`EmbeddingPort` — declared in `qfa.domain.ports`. Reuse them. Only introduce a
**new** port (and wire its adapter in the composition root, step 7) when the
endpoint needs an external dependency the orchestrator does not already hold;
most endpoints need none. The anonymisation round-trip and the
deadline/timeout/retry policy are documented under
[cross-cutting concerns](../architecture/04-crosscutting.md) — match them
rather than reinventing them.

## 4. Add the API schemas

HTTP request and response models live in `qfa.api.schemas`, separate from the
domain models so the wire contract can evolve independently of the core — see
[ADR-007: Separate API Schemas from Domain Models](../adr/007-separate-api-schemas.md).
Inherit the shared request bases (`ApiSingleInferenceRequestBase` for
per-record endpoints, `ApiBulkInferenceRequestBase` for bulk) so the new
endpoint picks up the common fields and validators.

```python
from pydantic import BaseModel, Field


class ApiClassifyRequest(ApiSingleInferenceRequestBase):
    labels: list[str] = Field(min_length=1, description="Candidate labels.")


class ApiClassifyResponse(BaseModel):
    label: str
    confidence: float | None = None
    request_id: str
```

Validation belongs at this boundary, not in the domain. Express bounds with
`Field(min_length=..., max_length=..., ge=..., le=...)`, and use
`@field_validator` to sanitise messy-but-recoverable input rather than
rejecting it — the existing `output_language` field is sanitised, never
refused. Keep the domain invariants strict and absorb the mess here, at the
edge.

Empty `content` is a deliberate non-error across every endpoint: a blank
record carries no information, so it is short-circuited to an empty result
rather than failing validation (issue #138). New per-record endpoints follow
the same rule — see step 5.

## 5. Declare the route

Routes live in `qfa.api.routes`, registered on the module-level `router` that
the app factory mounts. A handler is an `async def` decorated with the path,
response model, status code, and tag, and it declares its dependencies in the
signature.

```python
@router.post(
    "/v1/classify",
    response_model=ApiClassifyResponse,
    status_code=200,
    tags=["Inference"],
)
async def classify(
    body: ApiClassifyRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    _scope: CallContext = Depends(call_scope_for(Operation.CLASSIFY)),
) -> ApiClassifyResponse:
    """Assign one label to a feedback record.

    An empty ``content`` returns a 200 empty result with no LLM call.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    if not body.feedback_record.content:
        return ApiClassifyResponse(label="", confidence=None,
                                   request_id=request.state.request_id)

    domain_request = ClassificationRequestModel(
        feedback_record=FeedbackRecordModel(
            id=body.feedback_record.id,
            content=body.feedback_record.content,
            metadata=body.feedback_record.metadata,
        ),
        labels=tuple(body.labels),
        tenant_id=tenant.tenant_id,
    )
    result = await orchestrator.classify(domain_request, deadline)
    return ApiClassifyResponse(
        label=result.label,
        confidence=result.confidence,
        request_id=request.state.request_id,
    )
```

The three dependencies are the standard contract for an authenticated
inference route:

- `authenticate_request` validates the Bearer key and yields the
  `TenantApiKey` (security, below).
- `get_orchestrator` injects the orchestrator wired at startup.
- `call_scope_for(Operation.CLASSIFY)` opens the usage-tracking scope (step 6).

The route is also where the API ↔ domain mapping happens — never pass an API
schema into the orchestrator, and never return a domain model from the route.

### Documenting the endpoint

The route docstring is the source for the endpoint's OpenAPI entry, so it
carries the full semantics and edge cases. The
[REST API reference](../rest-api/index.md) carries a happy-path summary and the
field tables; add the new endpoint to both, in the same change.

## Security and authentication

Every endpoint except the liveness probe `GET /v1/health` requires an
`Authorization: Bearer <key>` header, per
[ADR-005: Bearer Token Authentication](../adr/005-bearer-auth.md). The
`authenticate_request` dependency enforces this and returns a `TenantApiKey`
carrying the `tenant_id` and an `is_superuser` flag.

- Inject the tenant into the domain request from `tenant.tenant_id`. Do not
  accept a tenant identifier from the request body — that would let a caller
  act as another tenant.
- For an administrative endpoint that must be restricted to privileged keys,
  depend on `require_superuser` instead of `authenticate_request`; it
  authenticates and then rejects non-superuser keys with a 403. The
  cross-tenant usage endpoints are the existing precedent.
- Authentication and authorisation failures are turned into the standard error
  envelope by the handlers registered in `qfa.api.app`; a route does not
  format them itself.

## 6. Wire usage and cost tracking

Cost is tracked per LLM call, not per endpoint, and the wiring is almost
automatic. Two edits connect a new endpoint:

1. Add a member to the `Operation` enum in `qfa.domain.usage_models` (for the
   example, `CLASSIFY = "classify"`). Operations are stored as plain strings,
   so no database migration is needed; never remove or renumber existing
   members, which would orphan historical rows.
2. Declare `call_scope_for(Operation.CLASSIFY)` as a route dependency, as in
   step 5.

That dependency opens a `call_scope` for the request, publishing a
`CallContext` (tenant, operation, request id) on a `ContextVar`. The
`TrackingLLMAdapter` — which decorates the real `LLMPort` in the composition
root — reads that context and records every LLM attempt, successful or failed,
into the `llm_calls` table with its token counts and computed cost. The
[call-context-and-usage-tracking section](../architecture/04-crosscutting.md#call-context-and-usage-tracking)
explains the correlation bridge, and the
[data model page](../architecture/05-data-model.md) documents the `llm_calls`
schema. An endpoint that fans out to several LLM calls records one row per
call, all sharing the request id, so per-invocation cost aggregates correctly.

The new operation then appears automatically in the responses of the existing
usage endpoints; no further work is needed there.

## 7. Wire a new dependency only if you need one

Skip this step for an endpoint that reuses the existing LLM and anonymisation
ports — the common case. The composition root
([`qfa.api.app` lifespan plus `qfa.api.composition`](../architecture/03-components.md#composition-root))
already constructs the orchestrator with everything an inference method needs.

Add to the composition root only when the endpoint requires an external
dependency the orchestrator does not yet hold. In that case: declare a new
port in `qfa.domain.ports`, implement an adapter that **explicitly inherits**
the port (the project requires the inheritance even though `Protocol`s allow
structural typing), and pass it into the orchestrator where it is built.

## 8. Map any new domain errors to HTTP

If the use case raises a domain error that is not already handled, register a
handler in `register_exception_handlers` in `qfa.api.app` so it produces the
shared error envelope with the right status code. Reuse an existing error
where the semantics match. The full table of domain-error-to-status mappings
is in the cross-cutting
[error handling](../architecture/04-crosscutting.md) section; keep it in sync
when adding a mapping.

## 9. Test across the tiers

Endpoint tests live under `tests/api/`. The suite is split by pytest marker,
and `make test` runs only the unit tier; integration and end-to-end tests are
excluded by default and gated on a running Postgres.

| Tier | Marker | Exercises |
|---|---|---|
| Unit | (none) | The route against `FakeOrchestrator` — no I/O, no real LLM |
| Integration | `integration` | Real Postgres (usage persistence and queries) |
| End-to-end | `e2e` | The full app with LiteLLM mocked via `respx` |

For a unit test, add a method for the new use case to `FakeOrchestrator` in
`tests/api/conftest.py`, then drive the route through the `client` fixture and
assert on status and body. Cover the success path, the missing/invalid key
(401), schema validation failure (422), and the empty-content short-circuit.
The [test seam](../architecture/03-components.md#test-seam) describes why the
fakes sit where they do. Every test function carries at least a one-line
docstring stating what it checks and why.

## 10. Verify and update the docs

Run the project gates before opening a pull request:

```bash
make test     # unit tier
make lint     # ruff, ty, and the import-linter layer contracts
make docs     # build the Sphinx site
```

`make lint` runs the import-linter contracts that enforce the layer
boundaries, so a route that reaches past its allowed imports fails here rather
than in review. Keep the documentation current in the same change: the route
docstring (OpenAPI), the [REST API reference](../rest-api/index.md), and this
guide if the procedure itself changes.
