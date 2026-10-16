## Problem

In `CodingService._judge_code_level` (`src/qfa/services/coding.py:461-469`), the method calls `self._judge_llm.complete(...)` without a `timeout` argument. Every other LLM call site in `coding.py`, `analyze.py`, and `summarize.py` first calls `LLMCallExecutor.check_deadline_and_get_timeout(deadline)`. Each of those sites then passes the result as `timeout=`. That method (`src/qfa/services/llm_call_executor.py:207-227`) shrinks the timeout as the deadline nears. This way, even a fully retried call finishes before the deadline. `_judge_code_level` only calls `_check_coding_deadline`. When the deadline already passed, that check raises an error. It does not bound the timeout of the call that follows.

Without an explicit timeout, `LLMPort.complete` falls back to its default of 20.0 seconds (`src/qfa/domain/ports.py:72`). `LLM_RETRY_BUDGET_MULTIPLIER` is 3.0 (`src/qfa/settings.py:18`). Because of this setting, one judge call can retry internally for up to 60 seconds, no matter how little of the request deadline remains.

`assign_codes` (`src/qfa/services/coding.py:318-332`) also has no concurrency limit on the judge calls it starts. `asyncio.gather` starts one `_judge_selected_path` task for each selected index. Each task then makes one `_judge_code_level` call for each hierarchy level in its path (up to 3 calls for a 3-level framework). `CodingResponse.selected` (`src/qfa/services/coding_classifier.py:60`) has no maximum length. A large coding framework, or a pick response that selects many paths, can therefore start many concurrent judge calls. `AnalyzeService.analyze_hierarchical` bounds its equivalent fan-out with one shared `asyncio.Semaphore` (`src/qfa/services/analyze.py:487-491`). `CodingService` has no semaphore anywhere.

## Trigger

If a coding framework has many options, or a pick step selects several paths, a request to `/v1/assign-codes` can start many concurrent judge calls. Each call can run for up to 60 seconds, independent of the remaining request deadline. The combination can push the whole request past the gunicorn worker timeout.

## Why this is not already handled

The codebase has a documented pattern for this (see `src/qfa/services/llm_call_executor.py:220-226` and the fix for #325). Every LLM call site must derive its timeout from `check_deadline_and_get_timeout`. Every concurrent fan-out must share one semaphore. `_judge_code_level` and `assign_codes` are the one call site and the one fan-out that do not follow this pattern.

## Suggested fix

- In `_judge_code_level`, call `check_deadline_and_get_timeout(deadline)` and pass the result as `timeout=`.
- In `assign_codes`, add a shared `asyncio.Semaphore`, or cap `valid_indices` before dispatch. This bounds concurrent judge calls to match `analyze_hierarchical`.
