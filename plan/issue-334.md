## Plan — #334: offload `summarize_bulk` anonymisation off the event loop

### Decisions the spec left open

**1. Offload only — no per-record chunking.** `analyze_bulk`'s fix was two changes: chunk per record (`LLMCallExecutor.anonymize_records_and_prompt`, #324) *and* offload via `asyncio.to_thread` (#325). This plan applies **only the offload**. Reasons:

- The spec's "Suggested fix" names the offload and nothing else, and its "Related" section routes anonymisation *speed* to #328.
- `summarize_bulk`'s envelope is built by `build_feedback_records_envelope` with `include_metadata=True`, so record metadata and ids are inside the text Presidio currently sees. `anonymize_records_and_prompt` redacts `record_text(record)` only and leaves metadata untouched. Switching would silently change *what leaves the service to the LLM provider* — a security-relevant behaviour change, not a concurrency fix, and it would need a `docs/security-brief.html` review.
- It would also change the judge's source text (`_build_judge_system_message(anonymized_user_message, …)`), so existing judge assertions would shift for no reason tied to this bug.

The builder should **not** reshape `summarize_bulk` into the analyze shape. One `anonymize` call over one envelope stays; it just stops running on the loop thread.

**2. No `max_length` on `feedback_records`.** The spec notes under "Problem" that `ApiBulkInferenceRequestBase.feedback_records` has no upper bound, but does not ask for one. `LLMCallExecutor.check_token_limit` already runs first in `summarize_bulk` (before any Presidio work) and rejects an over-cap batch with 413, which is the real size bound. A record-count cap is a new, breaking API contract on three bulk endpoints at once — out of scope. Leave `ApiBulkInferenceRequestBase` untouched.

**3. Call the executor, not the port.** Use `self._executor.anonymize_text(...)` rather than `self._anonymizer.anonymize(...)`. `summarize_bulk` already goes through the executor for `check_token_limit` and `deanonymize_json`, `SensitivityService` already uses `anonymize_text` for exactly this single-message shape, and keeping the offloaded call behind the executor matches how `docs/architecture/04-crosscutting.md` describes the split. Keep the `self._anonymizer` attribute — `summarize` and `summarize_community_meeting` still use it.

**4. Scope is `summarize_bulk` only.** `summarize` (summarize.py:372) and `summarize_community_meeting` (summarize.py:442) still anonymise synchronously on the loop. Both handle exactly one record of bounded size, so they are not the reported bug. Do not touch them; do not widen to `CodingService`.

**5. The spec at `spec/issue-334.md` has no acceptance-criteria checklist** (the file is 20 lines: Problem / Trigger / Suggested fix / Related). I treated the criteria as: anonymisation in `summarize_bulk` no longer runs on the event loop thread; a blocking anonymiser does not stall a concurrently-scheduled coroutine; the anonymise → call → de-anonymise round trip is unchanged. Step 3 proves the first two, step 4 the third.

Also note the spec's line references are stale against this worktree: the call is at `src/qfa/services/summarize.py:270-275`, not `201-207`, and the reference offload is `analyze.py:304-322`, not `281-290`. Same code, different lines.

---

### Steps

**Step 1 — add the two imports to `src/qfa/services/summarize.py`.**
Add `import asyncio` to the stdlib block (above `import logging`) and `from qfa.utils import timed` to the `qfa` import block, after `from qfa.services.record_links import hyperlink_form_references`. `qfa.services` → `qfa.utils` is already an allowed edge (`analyze.py:74`), so no `pyproject.toml` import-linter contract changes.

**Step 2 — replace the blocking call in `SummarizeService.summarize_bulk`** (`src/qfa/services/summarize.py`, currently lines 272-274). Replace:

```python
anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
    user_message
)
```

with a `timed()`-wrapped `await asyncio.to_thread(self._executor.anonymize_text, user_message)`, followed by the same content-free INFO log line `analyze_bulk` emits — `"anonymisation: %d record(s) in %.2fs"` with `len(request.feedback_records)` and `anonymize_sw.elapsed_seconds`. Use the same wording so one log query covers both bulk paths. Keep the comment short: why `to_thread` (synchronous CPU-bound Presidio pass would starve gunicorn's asgi-worker heartbeat, #325/#334) and that `to_thread` copies contextvars so `CallContext` still propagates. Do **not** repeat the chunking rationale from `analyze.py` — it does not apply here.

Ordering constraints inside the method, all of which must survive unchanged:
- `self._executor.check_token_limit(system_message, user_message)` stays **before** the anonymise call, on the un-anonymised message, so an over-cap batch still 413s without paying Presidio (pinned by `test_oversized_bulk_request_fails_before_anonymisation`).
- `self._executor.check_deadline_and_get_timeout(deadline)` stays **after** the anonymise call, so the LLM timeout reflects the time the offload actually consumed.

**Step 3 — update the `summarize_bulk` docstring** in the same file. Add one line to the existing prose: anonymisation runs off the event loop thread via `asyncio.to_thread`, so a large batch cannot stall gunicorn's asgi-worker heartbeat. The docstring must not grow beyond that one added fact. Do not copy `analyze_bulk`'s per-record paragraph.

Steps 1-3 are one edit to one file; land them together.

**Step 4 — add `TestAnonymizationOffEventLoop` to `tests/services/test_summarize.py`.**
Mirror `tests/services/test_analyze.py:277-320`. Add `import asyncio` and `import time` to the test module's imports.

- Test: `test_blocking_anonymizer_does_not_stall_concurrent_coroutines`.
- A local `BlockingAnonymizer(AnonymizationPort)` whose `anonymize` does a real `time.sleep(0.3)` and returns `(text, {})`; `anonymize_batch` returns `(texts, {})`; `deanonymize` returns text unchanged.
- `FakeLLMPort(responses=[_make_llm_response(structured=_make_aggregate_summary_result()), _make_llm_response(structured=_judge_text())])` — `summarize_bulk` reads `response.structured.summary`, so the first response must be an `AggregateSummaryResultModel`, not the default free-text payload.
- Build with `_build_service(fake_llm, settings, anonymizer=BlockingAnonymizer())`.
- Schedule a `ticker()` coroutine with `asyncio.create_task` that awaits `asyncio.sleep(0.05)` and records `time.monotonic()`; `await service.summarize_bulk(_make_aggregate_request(), _future_deadline())`; then `await ticker_task`.
- Assert the ticker finished in under `block_seconds / 2`. On `main` this test fails (the ticker cannot resume until the sleep returns); after step 2 it passes. **Write the test and confirm it fails before applying step 2**, so the offload is actually what makes it green.

`_build_service` passes the same anonymiser instance to both the service and the executor it constructs, so the blocking double is reached whichever of the two the implementation calls — the test does not over-fit to decision 3.

**Step 5 — add a bulk round-trip regression test to the same class or to the existing deanonymisation test group.**
`test_aggregate_summary_restores_redacted_values`: an anonymiser that returns `(text, {"<PERSON_0>": "Jane Doe"})` from `anonymize` and does a plain substring replace in `deanonymize`; the fake LLM's aggregate summary contains `<PERSON_0>`. Assert the returned `AggregateSummaryResultModel.summary` contains `Jane Doe`, and assert the user message the LLM received is the anonymised one. This pins that moving the call into a thread did not break the anonymise → call → de-anonymise sandwich or the mapping hand-off to `deanonymize_json`. The existing `test_summary_deanonymization_escapes_json_unsafe_characters` only covers the single-record `summarize` path, so the bulk path has no such cover today.

**Step 6 — documentation, in the same PR.**

- `docs/adr/003-fully-async-concurrency.md`. Two edits, both small; this ADR has been amended in place before (`8ee3c50`, `1c6db61`), so in-place is the established convention here.
  - The Consequences bullet at line ~77 says "the two narrow `asyncio.to_thread` call sites". There are already three (`analyze_bulk`, hierarchical anonymise, hierarchical embed) and this adds a fourth — drop the count rather than re-counting, e.g. "the narrow `asyncio.to_thread` call sites".
  - The "Accepted risk" paragraph names "`CodingService`/`SummarizeService`'s still-synchronous anonymisation on the main thread" as one way the shared `PresidioAnonymizer` gets entered concurrently. After this change `summarize_bulk` is off the main thread; `summarize`, `summarize_community_meeting` and `CodingService` still are. Correct the sentence to say that, and add `SummarizeService.summarize_bulk` to the list of offloaded call sites in the amendment's scope paragraph. The risk itself does not change — do not rewrite the analysis.
- `docs/architecture/04-crosscutting.md:29`. The bullet currently ends "single-message redaction calls the {py:class}`~qfa.domain.ports.AnonymizationPort` directly." That is no longer true for `summarize_bulk`. Rewrite the clause so it says single-message redaction goes through `LLMCallExecutor.anonymize_text`, and note that the bulk paths (`analyze_bulk`, `analyze_hierarchical`, `summarize_bulk`) run their redaction via `asyncio.to_thread`. Keep it to one bullet.
- `docs/operations/observability.md`, "Pipeline timing" section (line ~86). It says `analyze_bulk` (`mode=single_pass`) logs the `anonymisation: N record(s) in <seconds>s` line. Extend that sentence to name `summarize_bulk` too, since step 2 emits the identical line. One sentence, not a new section.
- No change to `docs/security-brief.html` (what is redacted and what reaches the provider is unchanged) and none to `docs/operations/langfuse-guide.html` (no trace, score, prompt or dataset change). `docs/architecture/03-components.md`'s service table describes call shape, not threading — leave it.

**Step 7 — verify.**
- `uv run pytest tests/services/test_summarize.py` first (fastest signal on steps 4-5).
- `make test` — in particular `tests/services/test_analyze.py` (shared offload expectations), `tests/api/test_routes.py` (the `/v1/summarize-bulk` cases at lines 395-490, 862-895, 1035-1064) and `tests/e2e/test_orchestrator_e2e.py`. Behaviour is unchanged, so every existing test must pass without edits — **if an existing test needs changing, that is a signal the implementation drifted into decision 1's territory; stop and re-read rather than editing the test.**
- `make lint` (`ruff`, `ty`, `lint-imports`).
- `make docs` at the repo root, to confirm the Sphinx cross-references in the edited pages still resolve.

**Step 8 — branch, commit, PR.**
Branch is already `fix/334-loop-bug-summarize-bulk-anonymizes-the-full-r` off `main`. Two conventional commits: `fix(summarize): run bulk anonymisation off the event loop` (steps 1-5) and `docs: record the summarize-bulk thread offload` (step 6) — or one combined `fix(...)` commit if the builder prefers; both are acceptable, docs must not land in a separate PR. No trailers of any kind in the message. Open the PR against `main` with `closes #334`, then request a human review — self-approval does not satisfy the "Review by at least one other human" ruleset, so do not merge.

### Ordering summary

Step 4's failing test comes before step 2's fix. Steps 1-3 are one atomic edit. Step 5 can be written at any point after step 2. Step 6 is independent of the code and can run in parallel. Step 7 gates step 8.
