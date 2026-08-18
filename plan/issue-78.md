# Implementation plan — issue #78

Verified against this worktree (`34476eb`, freshly based on `origin/main`). Every file, symbol and line the spec names exists; `#112` (the service extraction it depends on) is merged. The spec is buildable. Five places where it has drifted from the tree are called out under **Decisions** below, with the way I went.

---

## Decisions the spec left open (or where it is stale)

1. **The new ADR is `018`, not `017`.** `docs/adr/017-orchestrator-composition-only.md` already exists (landed with #112) and is referenced from `AGENTS.md`, `docs/adr/index.md` and `src/qfa/api/app.py:690`. Renumbering it is out of scope and would break those references. → File is `docs/adr/018-no-third-party-text-in-error-envelope.md`. Every "ADR-017" in the spec's prose means this new ADR; every "ADR-017" already in the tree means the composition ADR. The acceptance criterion "`docs/adr/017-*.md` exists" is satisfied in intent by `018-*`.

2. **`retry_after` extraction reads headers, never text.** Confirmed against the installed litellm 1.84.0 / openai 2.24.0: `RateLimitError` carries `.status_code` (429) and `.response` (an `httpx.Response` with `.headers`); `Timeout` carries `.status_code` (408) and `.headers` but **no** `.response`; `BadRequestError` 400, `APIError` whatever was passed. → `provider_status` comes from `getattr(exc, "status_code", None)`; `retry_after` from the `retry-after` response header parsed as a plain integer. The HTTP-date form of `Retry-After` is **not** parsed — it yields `None` and the handler's fallback applies. Rationale: date parsing buys nothing (Azure sends integer seconds) and adds a parse surface on provider-controlled input.

3. **`Retry-After` fallback is a module constant in `qfa.api.app`, value 30.** The handler cannot see the per-request `timeout` that sized the adapter's retry budget, so "derived from the retry budget" is resolved as ≈3× the adapter's backoff cap (`wait_exponential(max=10)`), documented in the constant's docstring. It lives in `qfa/api/app.py`, **not** in `qfa/settings.py`, so the "no new setting, no new field in `qfa.settings`" criterion holds literally. A provider-supplied value is clamped to `[1, 3600]` rather than discarded, so an absurd header can't produce a nonsense or negative header value.

4. **The four `except` clauses in `_complete_once` collapse into one.** All four litellm classes are `APIError` subclasses, so the spec's "one structured, content-free line" is achieved with a single `except (Timeout, RateLimitError, BadRequestError, APIError)` block plus a module-level `_to_domain_error()` dispatcher. This is also the single documented home for the Azure content-filter sniff. Behaviour is unchanged for every other litellm `APIStatusError` subclass (they already fell through to `LLMError`).

5. **The Release section of the spec is stale and should be ignored.** `pyproject.toml` is at `2.4.0`, not `2.1.0`; there is no `CHANGELOG.md` in the tree — python-semantic-release generates the version bump and changelog from conventional commits in the Release workflow (`docs/operations/release-flow.md`). → Do **not** hand-edit `project.version` and do **not** create a changelog file. One PR, one `feat:` commit, `closes #78`. The rest of the Release section (no migration page) stands.

Two smaller notes for the builder:

- The spec says line 343 raises `LLMError`; it actually raises `LLMResponseParseError` (an `LLMError` subclass, consumed by `CodingService` at `src/qfa/services/coding.py:271`). Keep that class — only the message changes. It maps to 502 `llm_error` by MRO fall-through, which is the spec's 502 row.
- `docs/architecture/04-crosscutting.md:96` currently documents `llm_unavailable` while the code emits `llm_error`. That stale row is inside the table this work rewrites, so it gets fixed here.

---

## Step 1 — Domain errors carry classified scalars

**Files:** `src/qfa/domain/errors.py`, `src/qfa/domain/__init__.py`

1. Give `LLMError` an `__init__(self, message: str, *, provider_status: int | None = None)` that calls `super().__init__(message)` (so `err.args == (message,)`) and stores `self.provider_status`. `LLMTimeoutError`, `LLMBadRequestError`, `LLMContentPolicyViolationError` and `LLMResponseParseError` inherit it unchanged.
2. Override on `LLMRateLimitError`: `__init__(self, message: str, *, provider_status: int | None = None, retry_after: int | None = None)`, delegating to `super().__init__(message, provider_status=provider_status)` and storing `self.retry_after`. Docstring must state the unit (**seconds**) and that `None` means "provider gave no usable header".
3. Add `class PromptInjectionDetectedError(AnalysisError)` under the analysis-errors section, with a one-line docstring pointing at `LiteLLMClient._check_injection` as the sole raiser.
4. Export `PromptInjectionDetectedError` from `src/qfa/domain/__init__.py` (import block and `__all__`, keeping alphabetical order).

**Tests — `tests/domain/test_errors.py`:** extend in the existing per-class style.
- `PromptInjectionDetectedError` is a subclass of `AnalysisError` and carries its message.
- `LLMError("x")` → `provider_status is None`; `LLMError("x", provider_status=500).provider_status == 500`.
- `LLMRateLimitError("x", provider_status=429, retry_after=17)` exposes both; both default to `None`.
- For each: `str(err) == "x"` **and** `err.args == ("x",)` — `args` is what the containment criterion asserts on, so pin it here.

**Ordering:** must land before steps 2 and 4.

---

## Step 2 — Adapter stops propagating litellm text

**File:** `src/qfa/adapters/llm_client.py`

1. Add two module-level helpers above `LiteLLMClient`:
   - `_provider_status(exc: Exception) -> int | None` — returns `exc.status_code` when it is an `int`, else `None`.
   - `_retry_after_seconds(exc: Exception) -> int | None` — reads `getattr(exc, "response", None).headers` falling back to `getattr(exc, "headers", None)`, returns `int(headers["retry-after"])` when that parses, `None` on anything else (missing header, HTTP-date, non-numeric). Docstring must say: *reads the header only, never the exception text*; unit is seconds.
   - `_to_domain_error(exc: APIError, provider_status: int | None) -> LLMError` — `isinstance` dispatch in this order: `Timeout` → `LLMTimeoutError`; `RateLimitError` → `LLMRateLimitError(..., retry_after=_retry_after_seconds(exc))`; `BadRequestError` → content-filter sniff, then `LLMContentPolicyViolationError` or `LLMBadRequestError`; else `LLMError`. All with **fixed** messages:

   | class | message |
   |---|---|
   | `LLMTimeoutError` | `"LLM provider timed out"` |
   | `LLMRateLimitError` | `"LLM provider rate limit exceeded"` |
   | `LLMContentPolicyViolationError` | `"LLM provider rejected the request under its content policy"` |
   | `LLMBadRequestError` | `"LLM provider rejected the request"` |
   | `LLMError` | `"LLM provider call failed"` |

   The sniff `"filtered" in msg and "content management policy" in msg` stays verbatim, with a comment naming it the one sanctioned read of a provider string (inspected, never propagated) and pointing at ADR-018.

2. Replace the four `except` blocks at `llm_client.py:221-236` with a single block that computes `provider_status`, emits exactly one content-free line, and re-raises with the chain preserved:

   ```python
   except (Timeout, RateLimitError, BadRequestError, APIError) as exc:
       provider_status = _provider_status(exc)
       logger.error(
           "LLM provider error: type=%s status=%s model=%s",
           type(exc).__name__, provider_status, self._model,
       )
       raise _to_domain_error(exc, provider_status) from exc
   ```

3. `_check_injection` (`llm_client.py:129-158`): raise `PromptInjectionDetectedError` instead of `AnalysisError`; keep the existing `logger.warning("Prompt injection detected: pattern=%s", ...)` and keep `pattern_name` in the exception message (in-repo-authored, useful in logs — the *handler* is what makes the response message constant). Update the `Raises` section of the docstring and the import at line 20.
4. Line 343: `LLMResponseParseError(f"LLM response validation failed for {response_model.__name__}")` — drop `: {exc}`, keep `from exc`. Keeping the words "validation failed" is deliberate: it leaves `tests/adapters/test_llm_client.py::test_structured_validation_error_mapped_to_llm_error` (`match="validation failed"`) passing unmodified.
5. Update the `Raises` section of `complete`'s docstring to name `LLMContentPolicyViolationError`, `LLMBadRequestError` and `PromptInjectionDetectedError`.

**Tests — `tests/adapters/test_llm_client.py`, new class `TestProviderTextContainment`** with `SENTINEL = "LEAK-CANARY-7f3a"`:
- Parametrized over the four litellm classes, each constructed with `message=f"boom {SENTINEL}"`, driven through `_complete_once` (no retry loop): assert the expected domain class, `SENTINEL not in str(err)`, `not any(SENTINEL in str(a) for a in err.args)`, and `err.__cause__ is not None`.
- A fifth case: `BadRequestError(message=f"The response was filtered due to the content management policy {SENTINEL}")` → `LLMContentPolicyViolationError` with the same three assertions. This is the "inspected, never propagated" proof.
- Scalars: `RateLimitError` built with `response=httpx.Response(429, headers={"retry-after": "17"}, request=...)` → `err.retry_after == 17`, `err.provider_status == 429`. Same without the header → `err.retry_after is None`.
- Pydantic: mock content `'{"invalid": "LEAK-CANARY-7f3a"}'` against `_StructuredResponse` → `str(err) == "LLM response validation failed for _StructuredResponse"`, `SENTINEL not in str(err)`, `err.__cause__ is not None`.
- caplog: drive the full `complete()` path with `RateLimitError(message=f"boom {SENTINEL}")` and `patch("qfa.adapters.llm_client.wait_exponential", return_value=wait_fixed(0.01))` + `timeout=0.01` (mirroring `TestLiteLLMClientRetry.test_retries_exhausted_reraises_domain_error`), under `caplog.at_level(logging.DEBUG)`. Assert no record's `getMessage()` contains the sentinel. This deliberately exercises tenacity's `before_sleep_log`, which interpolates `str(exc)` of the **domain** error — verified as clean only because of step 2. Docstring must record that `exc_info` tracebacks are exempt per ADR-018.

**Do not touch** `test_content_policy_bad_request_mapped` — it must pass unmodified (acceptance criterion).

---

## Step 3 — Usage repository stops storing the DSN

**File:** `src/qfa/adapters/usage_repository.py:70`

`raise UsageRepositoryUnavailableError("Usage repository is unavailable") from exc`. Update `_translate_db_errors`' docstring to state that the SQLAlchemy string (which embeds the DSN, password included when it is in the URL) is never stored on the domain error.

**Tests — new file `tests/adapters/test_usage_repository.py`** (unit, no DB — the existing `tests/integration/test_usage_repository.py` is excluded by the default `-m 'not integration and not e2e'` addopts, so the criterion could not be met there):
- `async with _translate_db_errors():` raising `OperationalError("SELECT 1", {}, Exception(f"could not connect to {SENTINEL}"))` → `UsageRepositoryUnavailableError` with the sentinel in neither `str(err)` nor `err.args`, and `err.__cause__ is not None`.
- Same for `InterfaceError`.

---

## Step 4 — API handlers: type-driven signal, constant messages

**File:** `src/qfa/api/app.py`

1. Add the module constant near the top:
   ```python
   RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS = 30
   ```
   with a docstring explaining it is used only when the provider sent no usable `Retry-After`, and that ≈3× the adapter's `wait_exponential(max=10)` backoff cap is long enough to outlast a burst the internal retry budget already failed to ride out. Not a setting (see Decision 3).
2. New handlers, each returning the standard `ApiErrorResponse` envelope with a **constant** message:

   | handler | exception | status | `code` |
   |---|---|---|---|
   | `_handle_content_policy_violation` | `LLMContentPolicyViolationError` | 422 | `content_policy_violation` |
   | `_handle_llm_rate_limited` | `LLMRateLimitError` | 429 | `llm_rate_limited` |
   | `_handle_llm_timeout` | `LLMTimeoutError` | 504 | `llm_timeout` |
   | `_handle_llm_error` *(existing, edited)* | `LLMBadRequestError`, `LLMError` | 502 | `llm_error` |
   | `_handle_prompt_injection_detected` | `PromptInjectionDetectedError` | 422 | `prompt_injection_detected` |

   `_handle_llm_rate_limited` sets `response.headers["Retry-After"]` from `max(1, min(exc.retry_after, 3600))` when `exc.retry_after` is a positive int, else `RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS`.
   `_handle_llm_error`'s log line becomes content-free: `logger.warning("LLM provider error: type=%s status=%s", type(exc).__name__, getattr(exc, "provider_status", None), exc_info=True)`. **Keep `exc_info=True`** — that is the accepted trade-off in ADR-018.
   Only `LLMError` is registered for the 502 row; `LLMBadRequestError` reaches it by MRO (verified: `starlette._exception_handler._lookup_exception_handler` walks `type(exc).__mro__`, so registration order is irrelevant and the most specific registered class always wins).
3. `_handle_analysis_error` (`app.py:395-432`): delete the `if "injection" in str(exc).lower():` branch **and** the sentence in the docstring that quotes `"injection"` — `grep -c '"injection"' src/qfa/api/app.py` must find nothing. What remains is 502 `analysis_unavailable` echoing `str(exc)`; add a one-line comment that this echo is safe only because every `AnalysisError`/`AnalysisTimeoutError` message is authored in-repo (verified across `services/analyze.py:335,472`, `services/coding.py:428,434`, `services/summarize.py:118,120,294`, `services/llm_call_executor.py:207,209` — all literals or literal + a formatted float).
4. `_handle_usage_repository_unavailable` (`app.py:462`): `logger.warning("Usage repository unavailable: error_class=%s", type(exc).__name__)`. The response body is already generic and does not change.
5. Register the five new/edited handlers in `register_exception_handlers`, importing `LLMBadRequestError`, `LLMContentPolicyViolationError`, `LLMRateLimitError`, `LLMTimeoutError`, `PromptInjectionDetectedError` from `qfa.domain.errors`.

**Tests — `tests/api/test_routes.py`, new class `TestErrorContractMapping`:**
- One `@pytest.mark.parametrize` over all six classes → exact `(status, error.code)` pairs: `LLMContentPolicyViolationError`→(422, `content_policy_violation`), `LLMRateLimitError`→(429, `llm_rate_limited`), `LLMTimeoutError`→(504, `llm_timeout`), `LLMBadRequestError`→(502, `llm_error`), `LLMError`→(502, `llm_error`), `PromptInjectionDetectedError`→(422, `prompt_injection_detected`). Drive via `test_app.state.analyze_service = FakeService(error=...)` and `POST /v1/analyze-bulk`, matching the existing `TestErrorMapping` style.
- Sentinel sweep: the same five LLM/injection classes constructed with a message containing `LEAK-CANARY-7f3a`, asserting `SENTINEL not in resp.text` — the **whole serialized body**, not `error.message`, so a future field addition cannot silently reintroduce the leak. Add a docstring noting `AnalysisError` is deliberately excluded from this sweep because its handler echoes in-repo-authored text by design (ADR-018).
- `Retry-After`, twice: `LLMRateLimitError("...", retry_after=17)` → header `"17"`; `LLMRateLimitError("...")` → header equal to `str(RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS)`. Both assert `int(resp.headers["retry-after"]) > 0`.
- Add the sentinel-body assertion for the 503 path in `tests/api/test_usage_routes.py`, reusing `_UnavailableUsageRepository` (line 384) with the sentinel as its message.

Existing tests that must keep passing unchanged: `test_502_analysis_error`, `test_summary_502_analysis_error`, `test_detect_sensitive_502_analysis_error` (`analysis_unavailable` is untouched) and `tests/e2e/test_orchestrator_e2e.py:191` (`LLMError` → 502).

**Ordering:** after step 1. Step 4 must be complete before step 5's mapping table is written, since the table is checked against the registered handlers.

---

## Step 5 — Documentation

1. **`docs/adr/018-no-third-party-text-in-error-envelope.md`** — follow the house structure (`# ADR-018: …` / Status: Accepted / Context / Decision / Consequences), modelled on `016`. Must state the principle (signal from the exception *type*; a provider string may be read exactly once at the adapter boundary to choose a domain class, never stored/returned/logged verbatim), both corollaries (asymmetric response-vs-log treatment; the log sink inherits the corpus's data classification because `raise … from exc` is kept), the `str(exc)`-echo carve-out for in-repo-authored messages, and the four rejected options from the spec's *Out of scope* section.
2. **`docs/adr/index.md`** — add the row to the index table **and** the entry to the hidden `toctree`. `make docs` runs `sphinx-build -W`, so a file missing from the toctree fails the build.
3. **`docs/architecture/04-crosscutting.md`** — rewrite the *Error → HTTP mapping* table (lines 88-99) so it matches `register_exception_handlers` exactly: drop the `AnalysisError (with "injection" in message)` row, add the five rows from step 4, and correct the stale `llm_unavailable` → `llm_error`. Add one sentence under the table: response messages for provider-derived errors are constants; detail lives in the logs (link ADR-018).
4. **`docs/operations/observability.md`** — under *Hard prohibitions*, add the two rules: (a) `exc_info`/`logger.exception` tracebacks may contain provider-controlled text via `__cause__`; that is deliberate (ADR-018), so log output is in scope for data classification and must not be exported to third-party log analytics without review; (b) a log line or handler may interpolate `str(exc)` only when the message was authored in this repo — third-party exception text is logged as `type=…`/`status=…` scalars instead.
5. **`docs/rest-api/index.md`** — in the *Error envelope* section, add a compact status/code table including 429 with `Retry-After` (integer seconds; honour it before retrying) and note that 5xx/429 messages are constant strings, with `request_id` as the handle for support.
6. **`docs/security-brief.html`** — `AGENTS.md` requires this file to be updated when anything security-related changes, and its line 302 ("Feedback content, prompts, model output and key values are never written to logs — enforced in code") is exactly the claim ADR-018 qualifies. Amend that bullet to say log *messages* never carry them, while diagnostic tracebacks may contain provider-returned text and are therefore classified with the corpus. This is an addition to the spec's Deliverables list, justified by the repo rule.

---

## Step 6 — Verification

Run and report, in order:

1. `make test` — full suite green, including the two adapter tests that must pass unmodified.
2. `make lint` — ruff (format + check), `ty`, and the import-linter contracts. No new cross-layer imports are introduced: `qfa.api.app` already imports `qfa.settings` and `qfa.domain.errors`; the adapter gains no new dependency.
3. `make docs` — must build clean under `-W`.
4. Spot-checks for the checklist items a test can't express:
   - `grep -c '"injection"' src/qfa/api/app.py` → no match.
   - `git diff src/qfa/settings.py` → empty (no new setting or env var).
   - `grep -n 'from exc' src/qfa/adapters/llm_client.py src/qfa/adapters/usage_repository.py` → the chain is preserved at all six translation sites (one collapsed litellm block covering four cases, one pydantic, one SQLAlchemy); the `__cause__ is not None` assertions in steps 2-3 cover this from the test side.
   - No route's success-path response shape changes; the only body field affected is `error.message`.

**Commit/PR:** one PR, one conventional-commit `feat:` (`feat(api): derive error signal from exception type, never provider text`), body `closes #78`. No version edit, no changelog file, no migration page.
