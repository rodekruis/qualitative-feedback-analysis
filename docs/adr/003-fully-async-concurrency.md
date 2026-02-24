# ADR-003: Fully Async Concurrency Model

## Status

Accepted

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
- No thread pool is used for request handling. Uvicorn's event loop handles
  all concurrency.

## Participants

- Domain expert (identified cancellation propagation issue)
- Devil's advocate (proposed async as strictly simpler)
- Architect (accepted the async model)
