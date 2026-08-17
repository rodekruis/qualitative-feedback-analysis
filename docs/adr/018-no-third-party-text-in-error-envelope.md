# ADR-018: No third-party text in the error envelope

## Status

Accepted

## Context

Third-party exception strings were reaching both the HTTP response body and
the logs from three sites: litellm provider exceptions
(`LiteLLMClient._complete_once`), pydantic `ValidationError` on a malformed
LLM response, and SQLAlchemy connectivity errors in
`SqlAlchemyUsageRepository`. All three call `str(exc)` and either return it
in a response `message` or log it verbatim.

That text is provider-controlled and unbounded. litellm embeds the
api_base/deployment URL, model name and Azure content-filter verdicts;
pydantic v2 `ValidationError` embeds `input_value` — the model's raw
response text, which `docs/operations/observability.md` lists as a hard
prohibition; SQLAlchemy connectivity errors routinely embed the DSN,
password included when it is in the URL.

Two complications rule out a narrow fix:

- **Sanitizing the message is not sufficient.** Every translation site
  raises `... from exc`, so the third-party exception stays attached as
  `__cause__`. Any handler that logs with `exc_info=True` or
  `logger.exception` emits the chained traceback, which carries the
  original text regardless of what the message says.
- **DEBUG-gating is not protective.** `LOG_LOGLEVEL` defaults to `DEBUG`
  for application packages, so "log the detail at DEBUG" ships on by
  default.

Removing the free text also removes real signal: no handler was registered
for `LLMRateLimitError`, `LLMBadRequestError` or
`LLMContentPolicyViolationError` — all three collapsed into a generic 502,
so the provider string was the only way a caller could distinguish "the
content filter rejected this" from "the provider is down."

## Decision

Error signal derives from the exception **type**, never from third-party
text. A third-party exception string may be **read** exactly once, at the
adapter boundary, to choose a domain error class. It is never stored on the
error, returned to a caller, or logged verbatim.

1. **Response bodies and logs get asymmetric treatment.** No provider text
   crosses the trust boundary into a response, ever — response messages for
   provider-derived errors are fixed, hand-written constants. Logs keep
   diagnostic detail (exception type, provider status code, model), because
   ops needs it to triage a 502.
2. **The log sink inherits the corpus's data classification.**
   `raise ... from exc` is kept for debuggability, which means a traceback
   emitted via `exc_info=True` / `logger.exception` may still contain
   provider-controlled text through `__cause__`. That is accepted
   deliberately: log output is in scope for data classification and must
   not be exported to third-party log analytics without review.
3. **A handler may echo `str(exc)` only when the message was authored in
   this repo.** `AnalysisError` / `AnalysisTimeoutError` messages are all
   literal constants (or a literal plus a formatted float) raised from
   `qfa.services`, so `_handle_analysis_error` echoing `str(exc)` stays
   safe. Provider-derived classes (`LLMError` and subclasses,
   `PromptInjectionDetectedError`, `UsageRepositoryUnavailableError`) never
   get this treatment — even their in-repo domain-error message is not
   echoed by the corresponding handler, so a diagnostic string kept for the
   logs (e.g. the injection pattern name) can't leak into a response by a
   future edit.
4. **Classified scalars replace free text where callers need signal.**
   `LLMError` carries `provider_status: int | None`; `LLMRateLimitError`
   additionally carries `retry_after: int | None`, read only from the
   provider's `Retry-After` response header — never from exception text.
   `PromptInjectionDetectedError(AnalysisError)` gives prompt-injection
   detection its own type instead of a substring match on the message.
5. **The Azure content-filter sniff is the one sanctioned read of a
   provider string.** `"filtered" in msg and "content management policy" in
   msg` inspects the litellm `BadRequestError` message to choose between
   `LLMContentPolicyViolationError` and `LLMBadRequestError` — but the
   string itself is never propagated into either error.

### Rejected options

- **Replacing the Azure content-filter sniff with structural detection**
  (litellm classification or `innererror.code`) — couples the adapter to
  provider internals for a bigger test surface, for a problem the string
  sniff already solves; out of scope for this change.
- **Denylist/regex scrubbing of provider strings**, in either sink — this
  is pattern-filtering of untrusted, unbounded, provider-controlled text
  and fails open on anything unanticipated.
- **A "verbose errors" opt-in setting** that echoes provider detail behind
  a flag — a switch that can be, and eventually will be, left on in
  production.
- **Severing the `from exc` chain** — the debuggability loss is not worth
  the containment, and it breaks the exception-chaining idiom used
  elsewhere in the codebase.

## Consequences

- New HTTP mapping: `LLMContentPolicyViolationError` → 422
  `content_policy_violation`; `LLMRateLimitError` → 429
  `llm_rate_limited` (with `Retry-After`); `LLMTimeoutError` → 504
  `llm_timeout`; `LLMBadRequestError` and `LLMError` → 502 `llm_error`;
  `PromptInjectionDetectedError` → 422 `prompt_injection_detected`. See
  `docs/architecture/04-crosscutting.md` for the authoritative table.
- `_handle_analysis_error`'s `"injection" in str(exc).lower()` routing is
  deleted — it was also a latent bug, since rewording the injection
  message would have silently turned a 422 into a 502.
- No new setting or environment variable: `RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS`
  is a module constant in `qfa.api.app`, not a field on `qfa.settings`.
- No response body field other than `error.message` changes shape, and no
  success-path response changes at all.

## When to revisit

- If a fourth translation site starts propagating third-party text, apply
  the same pattern: fixed message, classified scalars, `from exc` kept.
- If the Azure content-filter signal needs to become more precise than a
  substring match, that is the trigger to revisit the "rejected: structural
  detection" option above — not to loosen the propagation rule.

## Participants

Marius
