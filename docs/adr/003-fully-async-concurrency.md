# ADR-003: Fully Async Concurrency Model

## Status

Accepted (amended 2026-09-07 — see [Amendment](#amendment-2026-09-07-thread-offload-for-blocking-work) below)

## Context

The backend uses FastAPI, an async-native ASGI framework. The primary I/O
operation is calling the OpenAI API, which may take up to 2 minutes. The
orchestrator includes retry logic with backoff delays between attempts.

The architect initially proposed a synchronous orchestrator using
`time.sleep()` for backoff, called from async route handlers via
`asyncio.run_in_executor(None, ...)`.

The domain expert and devil's advocate identified several problems with
this approach:

1. **Cancellation does not propagate.** If the client disconnects or a
   gateway timeout fires, the async task is cancelled but the thread
   running the synchronous orchestrator continues, holding an LLM
   connection and burning resources.
2. **Thread pool sizing.** Each in-flight request occupies a thread. With
   a 2-minute timeout budget, a modest burst of 20 concurrent requests
   exhausts the default thread pool (40 threads), causing queuing delays
   that erode the timeout budget before the orchestrator even runs.
3. **Sync/async mixing.** If the LLM adapter uses the async OpenAI client
   (`AsyncOpenAI`), calling it from a synchronous orchestrator requires
   `asyncio.run()` inside the thread — creating a new event loop per call,
   which is an antipattern.

## Decision

The orchestrator, LLM client, and all I/O operations are fully async.

- `LLMPort.complete` is `async def`.
- `LLMClient` uses `openai.AsyncOpenAI` / `openai.AsyncAzureOpenAI`.
- `StandardOrchestrator.analyze` is `async def`, uses `asyncio.sleep` for
  backoff.
- Route handlers call `await orchestrator.analyze(...)` directly.

## Options Considered

### Option A: Sync orchestrator + run_in_executor (rejected)

- **Pro**: Simpler to reason about sequentially. `time.sleep` and synchronous
  exception handling are straightforward.
- **Con**: Cancellation issues, thread pool exhaustion, sync/async mixing
  bugs. The "simplicity" is illusory — the impedance mismatch creates subtle
  correctness problems.

### Option B: Fully async (chosen)

- **Pro**: Native cancellation propagation via `asyncio.Task.cancel()`.
  No thread pool sizing concerns. `asyncio.sleep` is non-blocking — other
  requests can be served during backoff. Idiomatic FastAPI.
- **Con**: Async test fixtures require `pytest-asyncio`. Slightly more
  ceremony in test setup.
- **Mitigation**: `pytest-asyncio` is lightweight and widely used.

### Option C: Hybrid — async route, sync LLM call in executor (not chosen)

- **Pro**: Keeps the orchestrator simple.
- **Con**: Same cancellation and thread pool problems as Option A, just
  with less code in the executor.

## Consequences

- All port interfaces define `async` methods.
- Tests for the orchestrator use `pytest-asyncio` and `async def` test
  functions.
- `asyncio.sleep` is patched in tests (not `time.sleep`).
- The `openai` SDK's async client (`AsyncOpenAI`) is used, which returns
  the same response types as the sync client.
- No thread pool is used for request handling, **except** the two narrow
  `asyncio.to_thread` call sites in the
  [2026-09-07 amendment](#amendment-2026-09-07-thread-offload-for-blocking-work).
  Uvicorn's event loop handles all other concurrency.

## Participants

- Domain expert (identified cancellation propagation issue)
- Devil's advocate (proposed async as strictly simpler)
- Architect (accepted the async model)

## Amendment (2026-09-07): thread offload for blocking work

`analyze_bulk` anonymised its whole concatenated corpus in one synchronous
Presidio call on the event loop thread. Presidio's within-call entity
dedup is quadratic in entity count, so a large corpus could block the loop
for tens of seconds — long enough to starve the `asgi` worker's heartbeat
coroutine (`gunicorn/workers/gasgi.py`, ticked from `await asyncio.sleep(1.0)`
in a loop) and get the process SIGKILLed by gunicorn's `--timeout` watchdog
(issue #325). `analyze_hierarchical`'s embedding step (`EmbeddingPort.embed`,
synchronous by design per [ADR-014](014-embedding-port-and-self-hosted-model.md))
carried the same risk.

This amends the "no thread pool is used for request handling" consequence:
`AnalyzeService.analyze_bulk` and `analyze_hierarchical` now run their
anonymisation (`LLMCallExecutor.anonymize_records_and_prompt`) and, for
`analyze_hierarchical`, their embedding (`EmbeddingPort.embed`) calls via
`asyncio.to_thread`, using Python's default thread pool executor rather than
a dedicated one. Scope is deliberately narrow — only these two known
CPU-bound, non-cancellable calls — not a general policy of moving
synchronous work off the loop:

- Both calls are one-shot and already complete by the time their `await`
  returns, so the cancellation-propagation problem that ruled out Option A
  above does not apply: cancelling the awaiting task does not need to stop
  work already running to completion in the thread.
- `asyncio.to_thread` copies the current `contextvars` context into the
  worker thread, so `CallContext` (`qfa.services.call_context`) keeps
  propagating to whatever the offloaded call logs or records.
- Onnxruntime's `session.run()` releases the GIL during inference, so
  embedding genuinely gains wall-clock concurrency from this, not just
  heartbeat liveness; Presidio does not release the GIL, so its offload is
  purely to keep the heartbeat alive on prd's 1 vCPU.
- Thread pool sizing is not a concern here the way it was for Option A:
  each in-flight request occupies at most one thread for the duration of
  one anonymise/embed call rather than the whole request, and prd runs a
  single gunicorn worker, so concurrent thread demand stays small.

**Accepted risk:** this is the first `asyncio.to_thread` usage in the
codebase, so it is also the first time the process-wide `PresidioAnonymizer`
(one `AnalyzerEngine`/`AnonymizerEngine` pair, shared by every service —
see the composition root) can be entered from more than one OS thread at
once — e.g. two concurrent `analyze_bulk` calls, or one overlapping with
`CodingService`/`SummarizeService`'s still-synchronous anonymisation on the
main thread. Presidio's maintainers describe the analyzer as "generally
thread-safe, though worth validating" (it delegates to spaCy, which
[explosion.ai built for exactly this multi-threaded pattern](https://explosion.ai/blog/multithreading-with-cython)),
without an explicit hard guarantee. We accept this risk rather than
serialising access with a lock, which would give back part of the
heartbeat-liveness benefit this amendment exists for. Revisit if
production sees anonymisation errors or corrupted placeholder mappings
under concurrent load.
