# Cross-cutting concerns

Things that don't belong to a single component — they show up at multiple layers.

## Anonymisation round-trip

For every operation that reaches the LLM, the orchestrator wraps the user-facing text in an anonymise → call → de-anonymise sandwich:

```mermaid
sequenceDiagram
    participant route as Route handler
    participant orch as Orchestrator
    participant anon as PresidioAnonymizer
    participant llm as LLMPort

    route->>orch: analyze(request, deadline)
    orch->>anon: anonymize(user_message)
    anon-->>orch: (anonymised_text, mapping)
    orch->>llm: complete(system_message, anonymised_text, …)
    llm-->>orch: structured response
    orch->>anon: deanonymize(response_json, mapping)
    anon-->>orch: response_with_pii_restored
    orch-->>route: result
```

Notes:

- The mapping lives in memory for the request and is discarded when the orchestrator method returns.
- The de-anonymise step runs over the serialised response — substitutions are textual, so the round-trip is a string replacement, not a structured walk.

## Call context and usage tracking

`qfa.services.call_context` defines two cooperating `ContextVar`s and their context-manager helpers:

- `current_request_id` / `request_id_scope(uuid)` — set per HTTP request by `RequestIdMiddleware` in `qfa.api.app`. The same UUID is returned in the `X-Request-ID` response header.
- `current_call_context` / `call_scope(tenant_id, operation)` — set per orchestrator invocation. Reads `current_request_id` and stamps it onto the `CallContext` as `call_id`, so the HTTP header, log lines, and `llm_calls.call_id` rows all share one UUID. Raises {py:exc}`~qfa.domain.errors.MissingRequestScopeError` if no request scope is active.

`call_scope` is entered by a FastAPI dependency at the driving-adapter layer, **not** by the orchestrator. Each route declares which operation it represents:

```python
async def analyze(
    ...,
    _scope: CallContext = Depends(call_scope_for(Operation.ANALYZE)),
): ...
```

`call_scope_for` lives in `qfa.api.dependencies`; it composes with `authenticate_request` to resolve the tenant and enters `call_scope` before the route body (and the orchestrator) runs. The orchestrator is therefore free of scope plumbing — pure use-case logic. The {py:class}`~qfa.adapters.tracking_llm.TrackingLLMAdapter` reads `current_call_context` when persisting each LLM call.

Non-HTTP callers (CLI, future jobs, ad-hoc tests) wrap their work in `request_id_scope(uuid4())` and `call_scope(...)` explicitly.

If `LLMPort.complete` is invoked outside an active `call_scope` (e.g. a wiring bug), `TrackingLLMAdapter` does **not** raise — it logs at ERROR, routes through to the inner LLM, and returns the response without persisting the attempt. Observability never breaks the use case; missing scope is loud in logs and alertable, but does not fail user-facing requests.

The flow — driving adapter (FastAPI) sets the ContextVars; driven adapter (`TrackingLLMAdapter`) reads them; the orchestrator in between is unaware:

```mermaid
sequenceDiagram
    participant mw as RequestIdMiddleware
    participant dep as call_scope_for(Op)
    participant route as Route handler
    participant orch as Orchestrator
    participant tla as TrackingLLMAdapter
    participant llm as Inner LLMPort
    participant repo as UsageRepository

    Note over mw,repo: Driving adapter sets ContextVars
    mw->>mw: set current_request_id = uuid4()
    mw->>dep: invoke FastAPI dependency
    dep->>dep: read current_request_id<br/>set current_call_context<br/>(tenant_id, operation, call_id)
    dep->>route: yield CallContext
    route->>orch: analyze(request, deadline)

    Note over orch,llm: Orchestrator is scope-unaware
    orch->>tla: complete(system_message, user_message, …)

    Note over tla,repo: Driven adapter reads ContextVar
    tla->>tla: ctx = current_call_context.get()
    tla->>llm: complete(…)
    llm-->>tla: LLMResponse
    tla->>repo: record_call(LLMCallRecord(<br/>tenant_id=ctx.tenant_id,<br/>operation=ctx.operation,<br/>call_id=ctx.call_id, …))
    tla-->>orch: LLMResponse
    orch-->>route: result
    route-->>mw: response
    mw->>mw: reset current_request_id<br/>(and X-Request-ID header)
```

The two ContextVars are reset on the way out (by `request_id_scope` and `call_scope` respectively), so successive requests in one event loop never leak state across each other.

## Deadlines, timeouts, retries

| Layer | Concern | Mechanism |
|---|---|---|
| Route handler | Per-request deadline | `deadline = now(UTC) + 120s`, passed as an absolute `datetime` into the orchestrator |
| Orchestrator | Deadline check | Before each LLM call: if remaining time is negative, raise {py:exc}`~qfa.domain.errors.AnalysisTimeoutError` |
| Adapter ({py:class}`~qfa.adapters.llm_client.LiteLLMClient`) | Retry on transient errors | `tenacity.retry` with exponential backoff (1s→10s, 60s budget) for {py:exc}`~qfa.domain.errors.LLMTimeoutError` and {py:exc}`~qfa.domain.errors.LLMRateLimitError` |
| Adapter ({py:class}`~qfa.adapters.llm_client.LiteLLMClient`) | Per-call timeout | Passed through to `litellm.acompletion(timeout=…)` |
| Adapter ({py:class}`~qfa.adapters.llm_client.LiteLLMClient`) | Token budget guard | Estimates `len(text) / chars_per_token`; raises {py:exc}`~qfa.domain.errors.FeedbackTooLargeError` if over `LLM_MAX_TOTAL_TOKENS` |

Retry policy and token budget belong to the adapter because both are model-specific (different LiteLLM-routed models have different context windows and rate-limit behaviour).

## Error → HTTP mapping

The exception handlers in `qfa.api.app` translate domain errors into HTTP responses:

| Exception | HTTP | `error.code` |
|---|---|---|
| Missing / invalid bearer token | 401 | `authentication_required` |
| Pydantic validation failure | 422 | `validation_error` |
| {py:exc}`~qfa.domain.errors.FeedbackTooLargeError` | 413 | `payload_too_large` |
| {py:exc}`~qfa.domain.errors.AnalysisTimeoutError` | 504 | `analysis_timeout` |
| {py:exc}`~qfa.domain.errors.AnalysisError` (with "injection" in message) | 422 | `prompt_injection_detected` |
| {py:exc}`~qfa.domain.errors.AnalysisError` (other) | 502 | `analysis_unavailable` |
| {py:exc}`~qfa.domain.errors.LLMError` | 502 | `llm_unavailable` |
| `UsageRepositoryUnavailableError` | 503 | `usage_backend_unavailable` |
| Usage tracking disabled | 503 | `usage_tracking_disabled` |
| Unhandled `Exception` | 500 | `internal_error` |

All responses share the same envelope shape with a server-generated `request_id`.

## Logging policy

Hard prohibitions — **never log at any level**:

- Feedback record content (`record.text` / `record.content`)
- User prompt (`request.prompt`) — log the character count instead
- Assembled system or user messages sent to the LLM
- LLM response text
- API key values (protected by `SecretStr`)

Safe to log: `request_id`, `tenant_id`, `operation`, record counts, estimated tokens, attempt numbers, model name, durations, HTTP status codes, `prompt_tokens`, `completion_tokens`, cost.

See [Observability](../operations/observability.md) for what each log statement looks like at runtime.
