# Components

The hexagonal layout has three ports, one application service, and a composition root that wires them together.

## Ports and adapters

```mermaid
flowchart LR
    orch["Orchestrator<br/>(qfa.services)"]

    subgraph llmport["LLMPort"]
        direction TB
        tracking["TrackingLLMAdapter<br/>(decorator)"]
        litellm["LiteLLMClient"]
        tracking --> litellm
    end

    subgraph anonport["AnonymizationPort"]
        presidio["PresidioAnonymizer"]
    end

    subgraph usageport["UsageRepositoryPort"]
        sqlrepo["SqlAlchemyUsageRepository"]
    end

    orch -->|complete| tracking
    orch -->|anonymize / deanonymize| presidio
    tracking -.->|record_call| sqlrepo
    routes_usage["/v1/usage<br/>route"] -->|get_usage_stats| sqlrepo
```

| Port | Adapter(s) | What it owns |
|---|---|---|
| `LLMPort` | `LiteLLMClient`; optionally wrapped by `TrackingLLMAdapter` when `DB_TRACK_USAGE=true` | One method, `complete(system_message, user_message, tenant_id, response_model, timeout)`. Returns `LLMResponse[T_Response]` carrying the structured output plus token counts and cost. |
| `AnonymizationPort` | `PresidioAnonymizer` | `anonymize(text) -> (text, mapping)` and `deanonymize(text, mapping) -> text`. The mapping is held in memory for the request lifetime, then discarded. |
| `UsageRepositoryPort` | `SqlAlchemyUsageRepository` | Writes one `LLMCallRecord` per LLM call (from `TrackingLLMAdapter`) and reads aggregate stats (from the `/v1/usage` routes). |

The tracking decorator is the only place hex's "stack adapters at the composition root" earns its keep — `TrackingLLMAdapter` is itself an `LLMPort`, so the orchestrator never knows whether tracking is on.

## The orchestrator

`qfa.services.orchestrator.Orchestrator` is one class with four async methods, each backing one HTTP endpoint:

| Method | Endpoint | What it does |
|---|---|---|
| `analyze` | `POST /v1/analyze` | One LLM call. Free-text summary of themes across submitted records. |
| `summarize` | `POST /v1/summarize` | One LLM call. Per-record summaries with a self-evaluated quality score. |
| `summarize_aggregate` | `POST /v1/summarize-aggregate` | Two LLM calls (summary + judge). Single aggregate summary with a calibrated score. |
| `assign_codes` | `POST /v1/assign_codes` | Multiple LLM calls per record: pick + judge at each level of a hierarchical coding framework. |

All four enter `call_scope(tenant_id, operation)` first — see [Cross-cutting concerns](04-crosscutting.md) for what that does.

## Composition root

`qfa.api.app.create_app()` builds the FastAPI instance; the `lifespan` context manager wires the dependency graph at startup. The wiring is roughly:

1. Load settings.
2. Construct the base `LLMPort` (a `LiteLLMClient`).
3. If `DB_TRACK_USAGE` is on:
   - Construct the `UsageRepositoryPort` (a `SqlAlchemyUsageRepository`).
   - Wrap the base `LLMPort` in a `TrackingLLMAdapter` that delegates to the inner port and records each call to the repository.
4. Construct the `AnonymizationPort` (a `PresidioAnonymizer`).
5. Construct the `Orchestrator` with the (possibly wrapped) ports.
6. Stash the orchestrator, API keys, and — when present — the usage repository on `app.state` for the request lifecycle to read.

This is the **only** place that knows about concrete adapter classes. Routes and dependencies read from `app.state` only.

## Test seam

`create_app(llm_factory=…)` lets end-to-end tests inject a `FakeLLMPort` without monkey-patching. The lifespan still runs — so the *real* `TrackingLLMAdapter`, `PresidioAnonymizer`, and migrations all execute. Only the bottom-most layer (the actual LLM call) is faked. See `tests/e2e/conftest.py`.
