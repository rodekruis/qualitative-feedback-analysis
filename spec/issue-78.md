Copilot identified a number of possible security issues in #64. This is one of them.

## Issue
Provider exception strings can contain sensitive details and are being propagated in error messages and logs. These should be treated as potentially sensitive and sanitized.

## TODO
- [x] Check if this is something that needs to be fixed
- [x] If so, come up with a fix — see the spec below
- [ ] Implement the spec

---

# Spec

## Findings

Confirmed, and the exposure is wider than the original description. Third-party exception text reaches both the HTTP response body and the logs from three distinct sites.

**1. LLM provider strings (`src/qfa/adapters/llm_client.py:206-235`)**

`_complete_once` catches litellm `Timeout` / `RateLimitError` / `BadRequestError` / `APIError`, calls `logger.error(exc)` (raw string, ERROR level), and re-raises domain errors carrying `str(exc)` verbatim. litellm builds those strings as `"litellm.BadRequestError: " + <provider response body>` (plus optional `litellm_debug_info`), so the content is provider-controlled and unbounded — routinely the api_base/deployment URL, model name, and Azure content-filter verdicts. `src/qfa/api/app.py:439-447` then puts that string into the response `message` field as a 502.

`docs/operations/observability.md` lists feedback text, prompts, assembled messages, LLM output, and key values as hard prohibitions. `logger.error(exc)` bypasses that policy *by construction*: it logs whatever the provider sends.

**2. Pydantic validation errors (`src/qfa/adapters/llm_client.py:343`)**

`LLMError(f"LLM response validation failed for {response_model.__name__}: {exc}")` is built from a pydantic `ValidationError`. Pydantic v2 error strings embed `input_value` — the model's raw response text. This is the most reliable leak of the three: it *always* carries model output, and that output is explicitly on the hard-prohibitions list. It currently goes straight into a 502 body.

**3. SQLAlchemy connectivity errors (`src/qfa/adapters/usage_repository.py:70`)**

`UsageRepositoryUnavailableError(str(exc))` from `OperationalError` / `InterfaceError`. Those strings routinely embed the DSN — host, port, user, database, and the password when it is in the URL. The handler already returns a generic message, but the raw string is still stored on the error and written by `logger.warning("Usage repository unavailable: %s", exc)`.

### Two non-obvious complications

**Sanitizing the message field is not sufficient.** The adapters raise `... from exc`, so the third-party exception stays attached as `__cause__`. Every site logging with `exc_info=True` or `logger.exception` emits the *chained* traceback, which contains the original provider message verbatim — `_handle_llm_error` (`logger.warning(..., exc_info=True)`), `_handle_analysis_error` (`logger.debug(..., exc_info=True)`), `_handle_unhandled_exception` (`logger.exception`). Message sanitization alone leaves the log leak fully intact.

**DEBUG-gating is not protective here.** `LOG_LOGLEVEL` defaults to `DEBUG` for application packages (`docs/operations/observability.md`), so "log the detail at DEBUG" would remain on in a default deployment.

### Sanitizing removes real signal today

No handler is registered for `LLMRateLimitError`, `LLMBadRequestError`, or `LLMContentPolicyViolationError` — all three collapse into `_handle_llm_error` → 502 `llm_error`. So today the *only* way a caller can distinguish "the content filter rejected your content" from "the provider is down" is by reading the leaky provider string. Removing the free text therefore has to be paired with structured signal, or we trade a security bug for a diagnosability regression.

## Principle (new ADR-017)

> Error signal derives from the exception **type**, never from third-party text. A third-party exception string may be **read** exactly once, at the adapter boundary, to choose a domain error class. It is never stored on the error, returned to a caller, or logged verbatim.

Two corollaries:

- **Response bodies and logs get asymmetric treatment.** No provider text crosses the trust boundary into a response, ever. Logs keep diagnostic detail, because ops needs it to triage a 502.
- **The log sink inherits the corpus's data classification.** We keep `raise ... from exc` for debuggability, which means tracebacks may contain provider-controlled text. That is accepted deliberately and must be written down: log output is in scope for data classification, and must not be exported to third-party log analytics without review.

A handler may echo `str(exc)` only when the message was authored in this repo. Verified: every `AnalysisError` / `AnalysisTimeoutError` message raised in `orchestrator.py` is a literal constant, so `_handle_analysis_error` echoing `str(exc)` stays safe.

## Changes

### `src/qfa/domain/errors.py`
- Add `PromptInjectionDetectedError(AnalysisError)`.
- `LLMError` subclasses accept classified scalars: `provider_status: int | None`, plus `retry_after: int | None` on the rate-limit class. Scalars, not free text — the invariant holds.

```python
class LLMRateLimitError(LLMError):
    def __init__(self, message: str, *,
                 provider_status: int | None = None,
                 retry_after: int | None = None) -> None: ...
```

### `src/qfa/adapters/llm_client.py`
- `_complete_once` raises domain errors with **fixed, hand-written** messages plus scalars. `from exc` is preserved.
- The Azure content-filter sniff (`"filtered" in msg and "content management policy" in msg`) **stays**, documented as the single sanctioned place a provider string may be inspected — inspected, never propagated. Replacing it with litellm/Azure internals is deliberately out of scope (couples us to their internals, bigger test surface).
- The four `logger.error(exc)` calls collapse into one structured, content-free line. It also fixes existing per-attempt log noise inside the retry loop:

```python
logger.error(
    "LLM provider error: type=%s status=%s model=%s",
    type(exc).__name__, provider_status, self._model,
)
```

- `_check_injection` raises `PromptInjectionDetectedError` instead of a bare `AnalysisError`.
- Line 343: drop the `{exc}` interpolation of the pydantic error; keep the response-model name only.

### `src/qfa/adapters/usage_repository.py`
- Line 70: fixed message; the DSN-bearing SQLAlchemy string is not stored on the error.
- The handler's `logger.warning("Usage repository unavailable: %s", exc)` becomes a structured, content-free line.

### `src/qfa/api/app.py`
Register per-class handlers with constant messages. Signal comes from the type, so the `"injection" in str(exc).lower()` routing in `_handle_analysis_error` is **deleted** — it is also a latent bug today, since rewording the injection message would silently turn a 422 into a 502.

| Exception | Status | `code` |
|---|---|---|
| `LLMContentPolicyViolationError` | 422 | `content_policy_violation` |
| `LLMRateLimitError` | 429 + `Retry-After` | `llm_rate_limited` |
| `LLMTimeoutError` | 504 | `llm_timeout` |
| `LLMBadRequestError`, `LLMError` | 502 | `llm_error` |
| `PromptInjectionDetectedError` | 422 | `prompt_injection_detected` |

Rationale for the status codes: 422 says "your content caused this, do not retry"; 429 makes standard client backoff libraries retry correctly; 504 is literally what happened once the retry budget is spent (and matches `AnalysisTimeoutError`); 502 remains "our request or the provider was broken". `Retry-After` is honest here because rate-limit errors only reach the handler *after* the internal retry budget is exhausted — read from the provider response headers when present, falling back to a constant derived from the retry budget.

Untouched: the 401 / 403 / 404 / 409 / 413 / 504-analysis handlers, whose messages are authored in-repo and carry useful detail (e.g. `FeedbackTooLargeError` reports the actual limit).

## Out of scope

- Replacing the Azure content-filter sniff with structural detection (litellm classification or `innererror.code`).
- Denylist/regex scrubbing of provider strings, in either sink — it is pattern-filtering of untrusted, unbounded, provider-controlled text and fails open on anything unanticipated.
- Any "verbose errors" setting that echoes provider detail on an opt-in flag: a switch that can be left on in production.
- Regenerating the checked-in `openapi.json` (no `make openapi` target exists; likely already stale).
- Severing the `from exc` chain — containment is not worth the debuggability loss, and it breaks the chaining idiom used elsewhere.

## Deliverables

- `docs/adr/017-no-third-party-text-in-error-envelope.md`
- `docs/architecture/04-crosscutting.md` — error → HTTP mapping table
- `docs/operations/observability.md` — traceback data-classification note, and the rule that a handler may echo `str(exc)` only for in-repo-authored messages
- `docs/rest-api/index.md` — 429 + `Retry-After`
- `tests/adapters/test_llm_client.py` — exception → domain class mapping, and assert a sentinel provider string is absent from the domain error
- `tests/api/test_routes.py` — status + `code` per class, and assert a sentinel string injected into the raised exception never appears in the response body

## Acceptance criteria

Every item below is checkable by running something. "**Sentinel**" means a unique marker string (e.g. `LEAK-CANARY-7f3a`) planted inside the third-party exception that the test raises — the leak checks assert on its absence, so they cannot pass vacuously.

### Containment

- [ ] For each of litellm `Timeout`, `RateLimitError`, `BadRequestError`, `APIError` whose string contains the sentinel: the resulting domain error has the sentinel in neither `str(err)` nor `err.args`. (4 cases.)
- [ ] A malformed LLM response containing the sentinel, failing `model_validate_json`, produces an `LLMError` whose message contains neither the sentinel nor any fragment of the response payload — only the response-model name.
- [ ] A SQLAlchemy `OperationalError` carrying the sentinel as its DSN produces `UsageRepositoryUnavailableError` with the sentinel in neither `str(err)` nor `err.args`.
- [ ] For every endpoint that can raise the above, the sentinel appears nowhere in the **full serialized response body** — assert against the whole JSON string, not just `error.message`, so a future field addition cannot reintroduce the leak silently.
- [ ] With `caplog` set to DEBUG (the project default `LOG_LOGLEVEL`), the LLM failure path emits **no** log record whose `getMessage()` contains the sentinel. Provider text reachable only via the `exc_info` traceback is exempt — that is the accepted trade-off recorded in ADR-017.

### Contract

- [ ] A parametrized API test covers every row of the mapping table — all 6 exception classes, including both classes on the 502 row — asserting the exact (status, `code`) pair.
- [ ] A 429 response carries a `Retry-After` header whose value parses to a positive integer, verified twice: once where the provider supplies the header, once where it does not (fallback path).
- [ ] `PromptInjectionDetectedError` returns 422 `prompt_injection_detected`, and `grep -c '"injection"' src/qfa/api/app.py` returns `0`.
- [ ] Azure content-filter detection still triggers on the `filtered` + `content management policy` string: the existing adapter test for this passes **unmodified**.

### No regression, no scope creep

- [ ] `make test` and `make lint` both pass, import-linter contracts included.
- [ ] `raise ... from exc` is preserved at every translation site (4 litellm, 1 pydantic, 1 SQLAlchemy) — verifiable by asserting `err.__cause__ is not None` in the tests above.
- [ ] No new setting or environment variable is introduced: the diff adds no field to `qfa.settings`. Keeps the rejected "verbose errors" flag rejected.
- [ ] No response body field other than `message` changes shape, and no endpoint's success-path response changes at all.

### Documentation

- [ ] `docs/adr/017-*.md` exists and is listed in `docs/adr/index.md`.
- [ ] The error → HTTP table in `docs/architecture/04-crosscutting.md` matches the handlers actually registered in `register_exception_handlers` — same classes, same statuses, same codes, no stale rows.
- [ ] `docs/operations/observability.md` states both the traceback data-classification caveat and the "echo `str(exc)` only for in-repo-authored messages" rule.
- [ ] `make docs` builds successfully.

## Release

One PR, conventional-commit `feat`, closing this issue. `2.1.0` → `2.2.0`, CHANGELOG entry only, **no migration page**: no in-repo consumer parses error bodies (the Power Automate and EspoCRM scripts do not touch them), and 502 → 422/429/504 only narrows a failure clients already handled — it makes their retry logic more correct, not less.

Depends-on: #112 
