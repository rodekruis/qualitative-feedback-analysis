# Implementation plan — #333

## Decisions the spec left open

**1. Semaphore, not a cap on `valid_indices`.** The spec offers either. Capping silently drops code paths the model picked, and the ranking in `assign_codes` needs every candidate judged before `max_codes` truncates — a pre-dispatch cap would change which codes get returned. The semaphore changes no output, only pacing. Going with the semaphore.

**2. Both halves land on one call, via `LLMCallExecutor.bounded_complete`.** `bounded_complete` (`src/qfa/services/llm_call_executor.py:156`) already is "acquire the semaphore, then derive the timeout from the deadline, then call". Deriving the timeout *after* the acquire is strictly better than `check_deadline_and_get_timeout(deadline)` before it — a judge call that queued behind siblings gets a window reflecting the budget that actually remains. It is exactly what `AnalyzeService._judge_chunk` (`src/qfa/services/analyze.py:841`) does, down to the `with judge_call():` wrapper and the `llm=self._judge_llm` override. So `_judge_code_level` does not grow its own `check_deadline_and_get_timeout` call; it delegates.

**3. The semaphore is per request, created in `assign_codes` — not a `CodingService` instance attribute.** Matches `analyze_hierarchical` (`src/qfa/services/analyze.py:534`). An instance-level `asyncio.Semaphore` binds to the first event loop that touches it (`_LoopBoundMixin`), which breaks any test or script that reuses one service across loops, and would serialise unrelated concurrent requests against each other. Step 6's second test pins this.

**4. New `CodingSettings` group with `CODING_MAX_CONCURRENT_JUDGES`, default `8`.** Alternatives were a bare module constant (not operable — ops cannot turn it down when the provider rate-limits) or reusing `ANALYZE_MAX_CONCURRENT_CHUNKS` (wrong group; `AnalyzeSettings`' own docstring says per-endpoint tuning lives in its own class so the env-var names never need renaming). Default `8` rather than analyze's `64`: `/v1/assign-codes` is a single-record endpoint on a 240s deadline, and a normal pick selects 1–3 paths, so 8 leaves the common case untouched while bounding a pathological pick. It is a one-line change if ops wants it higher.

**5. `_check_coding_deadline` stays in `_judge_code_level`.** It is redundant with what `bounded_complete` raises, but it fires *before* the token-limit check and before queueing, and it carries the use-case-specific message. Removing it is churn with no payoff.

**6. Not fixing, deliberately —** call these out in the PR body rather than changing them:
- `asyncio.gather` in `assign_codes` has no `return_exceptions=True`, so one path raising `AnalysisTimeoutError` fails the whole request and orphans the sibling tasks. That is today's behaviour; queueing makes starvation *more* reachable, but turning a 504 into partial results is a user-visible product decision that was not signed off here.
- `docs/operations/settings-reference.md:152-153` has drifted from the code (documents `8`/`4000`, the code has `64`/`2000`). Unrelated to this fix; worth its own issue.
- `_judge_code_level` calls `self._anonymizer.anonymize` directly rather than `self._executor.anonymize_text`. Pre-existing, out of scope.

**7. Behaviour change worth naming in the PR body:** `check_deadline_and_get_timeout` raises `AnalysisTimeoutError` when under `_MINIMUM_ATTEMPT_WINDOW` (10s) remains, where `_check_coding_deadline` only raised at 0s. After this change a judge call with 6s of budget left raises instead of issuing a call that cannot finish. That is the point of the pattern and it matches every other call site, but it is a real change: `/v1/assign-codes` will now 504 slightly earlier in a time-starved request instead of overrunning the gunicorn worker.

---

## Steps

Steps 1–2 are plumbing with no behaviour change. Step 3 is the timeout half of the bug. Steps 4–5 are the concurrency half. **Step 3 must land before step 5** (step 5 replaces the exact line step 3 writes). Suggest three commits — `feat(settings)` for 1–2, `fix(coding)` for 3, `fix(coding)` for 4–5 — so the timeout fix is reviewable in isolation if the semaphore half draws discussion.

### 1. Add `CodingSettings` to `src/qfa/settings.py`

Add the class after `AnalyzeSettings` (which ends at line ~301), mirroring its shape:

- `model_config = SettingsConfigDict(env_prefix="CODING_")`
- `max_concurrent_judges: int = Field(default=8, ge=1, description=...)` — the description should say what it bounds (concurrent per-level judge calls across all selected paths within one request), why (provider rate limit / fan-out is `len(selected) × levels` and `CodingResponse.selected` has no max length), and that `1` is fully sequential. Follow `AnalyzeSettings.max_concurrent_chunks`' wording.
- Class docstring: one line on scope, plus the per-endpoint-group rationale that `OrchestratorSettings` already states.

Add `coding: CodingSettings = Field(default_factory=CodingSettings)` to `AppSettings` (`src/qfa/settings.py:444`), placed next to `analyze:`.

**Tests —** `tests/test_settings.py`, alongside `test_analyze_settings_have_clustering_and_trend_fields` (line 341):
- `test_coding_settings_defaults_and_env_prefix(monkeypatch)` — default is `8`; `CODING_MAX_CONCURRENT_JUDGES=3` is read; `CODING_MAX_CONCURRENT_JUDGES=0` raises `ValidationError` (pins `ge=1`).
- In `TestAppSettings` (line 376), assert `AppSettings().coding` is a `CodingSettings` with the default cap.

No `docs/python-api` edit needed — the autosummary at `docs/python-api/index.md` is `:recursive:`.

### 2. Inject `CodingSettings` into `CodingService`

`src/qfa/services/coding.py`:
- Import `CodingSettings` from `qfa.settings` (`qfa.services` → `qfa.settings` is already an allowed edge; `analyze.py:73` does it).
- Add `coding_settings: CodingSettings | None = None` as the last `__init__` keyword (keeps the three existing test construction sites and `build_services` compiling unchanged), and `self._coding_settings = coding_settings or CodingSettings()` — same `or`-default idiom as `analyze.py:240`.
- Add the `coding_settings` entry to the class's numpy `Parameters` block, two lines max: what it configures and that `None` reads the process environment.

`src/qfa/api/composition.py:504` — pass `coding_settings=settings.coding` in the `CodingService(...)` call inside `build_services`.

**Test —** `tests/api/test_composition.py`: assert `graph.coding._coding_settings is settings.coding`, matching the existing private-attribute wiring assertion at line 367.

### 3. Give the judge call a deadline-derived timeout

`src/qfa/services/coding.py`, `_judge_code_level` (line ~461). Replace the bare `await self._judge_llm.complete(...)` with:

```python
with judge_call():
    response = await self._executor.complete(
        llm=self._judge_llm,
        system_message=system_message,
        user_message=user_message,
        tenant_id=tenant_id,
        response_model=str,
        deadline=deadline,
        prompt=prompt_ref(self._prompt_versions, CODING_CLASSIFIER_JUDGE),
    )
```

`LLMCallExecutor.complete` (`src/qfa/services/llm_call_executor.py:269`) derives the timeout and forwards `prompt` unchanged, so tracking, Langfuse prompt-version tagging and judge-connection routing are all untouched. Leave the preceding `_check_coding_deadline` / `check_token_limit` / `anonymize` lines where they are.

Add to the method docstring's `Raises` a line for `AnalysisTimeoutError` when under 10s of budget remains — that is the added fact the signature doesn't carry (decision 7).

**Tests —** `tests/services/test_coding.py`, new class `TestJudgeCallTimeout`. `FakeLLMPort.calls` already records `timeout` (`tests/services/test_summarize.py:213`), so no new double is needed.
- `test_judge_calls_carry_a_deadline_derived_timeout` — deadline 30s out, `LLM_TIMEOUT = 30.0`, `LLM_RETRY_BUDGET_MULTIPLIER = 3.0` → every recorded call's `timeout` is `pytest.approx(10.0, abs=0.5)`. Fails before this step (the judge call records `FakeLLMPort`'s signature default of `40.0`).
- `test_judge_timeout_is_capped_by_the_per_call_limit` — deadline 300s out → judge `timeout == 30.0` (the `llm_timeout_seconds` cap binds, not the deadline). The two together pin the `min(cap, remaining/multiplier)` shape without duplicating `test_llm_call_executor.py`'s arithmetic tests.
- Both must assert the **judge** call specifically, not just the pick — select the recorded calls by `response_model is str` (the pick uses `CodingResponse`).

### 4. Thread a per-request semaphore through the judge fan-out

`src/qfa/services/coding.py`:
- In `assign_codes`, immediately before the `asyncio.gather` at line ~318:
  ```python
  # One semaphore per request bounds every judge call across all selected
  # paths, as analyze_hierarchical does for its map/judge/reduce fan-out.
  # Per request, not per service: an instance-level semaphore would bind to
  # one event loop and serialise unrelated requests against each other.
  semaphore = asyncio.Semaphore(self._coding_settings.max_concurrent_judges)
  ```
  and pass `semaphore=semaphore` into each `self._judge_selected_path(...)` call.
- `_judge_selected_path` — add a keyword-only `semaphore: asyncio.Semaphore` parameter and forward it to `_judge_code_level`. One docstring line: it bounds the per-level calls, which are sequential within a path but concurrent across paths.
- `_judge_code_level` — add the same keyword-only parameter.

`asyncio` is already imported.

### 5. Put the judge call under the semaphore

Swap step 3's `self._executor.complete(...)` for `self._executor.bounded_complete(semaphore, llm=self._judge_llm, ...)` — identical keywords otherwise. This is the same call shape as `analyze.py:841`. Do not pass `timing=`: coding has no per-chunk debug logging to split queue-wait out of, and `SlotTiming` would be collected and dropped.

**Tests —** `tests/services/test_coding.py`, new class `TestJudgeConcurrency`. Add a `ConcurrencyTrackingLLM(LLMPort)` local to this module, modelled on `tests/services/test_analyze_hierarchical.py:616-660`: increment `in_flight`, record `peak_in_flight`, `await asyncio.sleep(0)` (the yield is what lets the gathered tasks interleave — without it every coroutine runs to completion and the peak reads 1 regardless), decrement in a `finally`, then answer a `CodingResponse` when `response_model is CodingResponse` and `_judge_text(0.9, "...")` when it is `str`. Use a framework of ~6 single-level root codes and a pick selecting all of them.

- `test_judge_calls_run_concurrently_bounded_by_the_setting` — `max_concurrent_judges=2`; assert `len(judge_calls) >= 3` (proves a real fan-out) **and** `peak_in_flight == 2` (proves both that concurrency happens and that it is bounded). Fails before this step with `peak_in_flight == 6`.
- `test_max_concurrent_judges_one_is_fully_sequential` — cap `1` → `peak_in_flight == 1`, mirroring `test_max_concurrent_chunks_one_is_fully_sequential`.
- `test_the_bound_is_per_request_not_per_service` — one service, cap `1`, two `assign_codes` calls under one `asyncio.gather` → `peak_in_flight == 2`. Pins decision 3; an instance-level semaphore would make this `1`.

The pick call goes through `executor.complete` and is *not* under the semaphore, which is correct (it is a single call that must finish before any judge starts) and does not disturb the peak measurement, since pick completes before the fan-out opens.

### 6. Documentation (same PR, per AGENTS.md)

- `docs/operations/settings-reference.md` — new `## Coding (`CODING_*`)` section between Analyze (line 142) and Auth (line 157), one table row for `CODING_MAX_CONCURRENT_JUDGES` in the existing `| Variable | Required | Default | Notes |` format. Note: `1` = sequential.
- `docs/architecture/04-crosscutting.md`, "Deadlines, timeouts, retries" table (line 79) — add one row: *Application service | Concurrent LLM fan-out | One per-request `asyncio.Semaphore` bounds every concurrent LLM call in a fan-out — `ANALYZE_MAX_CONCURRENT_CHUNKS` for hierarchical analyse, `CODING_MAX_CONCURRENT_JUDGES` for the assign-codes per-level judges.* One row, not a prose section — the surrounding table is the right altitude and the deadline rows above already state the per-call rule this completes.
- **No change** to `docs/security-brief.html` (no data flow, auth surface or PII handling changes) or `docs/operations/langfuse-guide.html` (same traces, same `prompt_ref`, same `record_coding_judge_score` calls — `bounded_complete` routes to the same client object). Say so in the PR body so the reviewer knows both were considered.

### 7. Verify

- `make test` and `make lint` (`lint` runs the `import-linter` contracts — `qfa.services` → `qfa.settings` is an existing allowed edge, so step 2's import should not trip them; if it does, stop and report rather than editing the contracts).
- `make docs` — Sphinx runs with `-W`, so a malformed table or a bad cross-reference in step 6 fails the build.
- Confirm `tests/services/test_orchestrator_judge_routing.py` and `tests/services/test_prompt_registry.py` still pass unchanged: both construct `CodingService` without the new keyword and both assert judge calls land on the judge connection, which `llm=self._judge_llm` preserves.

### 8. PR

Branch is already `fix/333-loop-bug-assign-codes-judge-calls-skip-the-de` off `origin/main`. Conventional-commit subjects, no trailers. PR body closes #333 and states decisions 1, 4, 6 and 7 explicitly — 7 in particular, since it is the one caller-visible change a reviewer could otherwise miss. `main` requires one approving human review; request it and wait.
