# ADR-001: Use Pydantic for Domain Models

## Status

Accepted

## Context

The project follows hexagonal architecture. A core tenet of hexagonal
architecture is that the domain layer should be free of framework
dependencies so that it remains testable and portable.

The architect initially proposed frozen `dataclasses` for all domain entities
(`FeedbackDocument`, `AnalysisRequest`, `AnalysisResult`, `LLMResponse`,
`TenantApiKey`) to keep the domain layer framework-free. Pydantic would only
be used at the API boundary in `api/schemas/`.

This would require a translation layer: every incoming request must be
converted from a Pydantic API schema to a domain dataclass, and every
outgoing result converted back. This doubles the number of data definitions
and adds boilerplate mapping code.

## Decision

Use Pydantic `BaseModel` with `model_config = ConfigDict(frozen=True)` for
all domain entities.

## Options Considered

### Option A: Frozen dataclasses in domain (rejected)

- **Pro**: Domain has zero framework dependencies. Fully portable.
- **Con**: Requires a parallel set of Pydantic schemas at the API boundary.
  Every field is defined twice. A translation layer must be written, tested,
  and maintained. For a v1 with no domain logic (entities are pure DTOs),
  this is overhead without payoff.

### Option B: Pydantic everywhere (chosen)

- **Pro**: One set of data definitions. Validation, serialization, JSON Schema
  generation, and immutability all come free. No translation boilerplate.
- **Con**: Domain layer depends on Pydantic. If Pydantic introduces a
  breaking change, the domain is affected.
- **Mitigation**: Pydantic v2 has a stable API. The coupling is to a data
  validation library, not to a web framework. The domain does not use FastAPI,
  uvicorn, or HTTP concepts — only `BaseModel` and `Field`.

### Option C: attrs or plain classes (not considered)

Would introduce a third-party library for a role Pydantic already fills.

## Consequences

- Domain entities are Pydantic models. They can be used directly in tests
  without any factory or conversion.
- API schemas (`api/schemas.py`) remain separate from domain models — they
  define the public API contract and may diverge from internal models over time
  (see [ADR-007](007-separate-api-schemas.md)).
- If a future requirement demands a framework-free domain (e.g., extracting
  the domain into a shared library), the migration path is: replace
  `BaseModel` with `dataclasses` and add the translation layer at that point.

## Participants

- Architect (proposed dataclasses)
- Devil's advocate (proposed Pydantic, accepted)
- Domain expert (supported Pydantic for pragmatic reasons)
