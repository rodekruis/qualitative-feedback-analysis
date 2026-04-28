# Cost and Endpoint Usage Tracking

**Issue:** #5 — \[FEATURE] expose cost per user to API endpoint (extended scope)
**Branch:** `feat/token-usage-tracking`
**Date:** 2026-04-28
**Supersedes:** the "token tracking only — cost deferred" scoping note in the issue thread. Cost is back in scope, plus a new `operation` (endpoint) dimension.

## Problem

The backend has no per-tenant, per-operation visibility into LLM usage or cost. We cannot answer:

1. What did each tenant cost us in a given period?
2. Which orchestrator operations are most-used?
3. Which orchestrator operations are most-expensive?

A previous planning round on issue #5 established a token-only `llm_calls` table, a `TrackingOrchestrator` decorator, and the `/v1/usage` + `/v1/usage/all` endpoints. That implementation:

- Records **one summed row per orchestrator call** — collapsing multi-LLM-call operations and forcing a synthetic `model` value when an operation spans models.
- Has **no cost column**.
- Has **no operation/endpoint dimension** — every row is anonymous as to which user-visible operation produced it.
- Does not record **failed LLM attempts**, so the bill-vs-DB delta caused by retries and mid-stream errors is invisible.

This spec defines the target shape that addresses all four gaps.

## Decisions summary

| Decision | Choice |
|---|---|
| Endpoint dimension | Orchestrator **operation** (`analyze`, `summarize`, `summarize_aggregate`, `assign_codes`) — not HTTP route, not LLM-call sub-operation |
| Recording granularity | **One row per LLM call attempt** (success or failure), recorded at the port layer |
| Where recording happens | **`TrackingLLMAdapter`** — a decorator wrapping `LLMPort`, mirroring the existing `TrackingOrchestrator` pattern at the orchestration layer |
| How `tenant_id` + `operation` reach the tracker | **`ContextVar[CallContext]`** set by an `async with self._call_scope(req, operation=...)` at each public orchestrator method entry; read by `TrackingLLMAdapter`; raises `MissingCallScopeError` if unset |
| Failure recording | Every attempt — `status` + `error_class` columns; `cost_usd` and token totals scoped to `status='ok'` |
| Cost source | LiteLLM `response_cost` (and `litellm.completion_cost(response)` fallback); `Decimal` end-to-end; serialized as JSON number |
| API shape | **Extend** existing `/v1/usage` and `/v1/usage/all` (option P) with `total_cost_usd`, `failed_calls`, `by_operation`, and `?from`/`?to` time filtering |
| Per-operation row | `total_calls`, `failed_calls`, `cost_usd`, `input_tokens_total`, `output_tokens_total` (option ii — totals + failure visibility) |
| Aggregate scope | Cost and token totals over `status='ok'` rows only; `total_calls`/`failed_calls` count all rows (policy α) |
| Indexes | `(tenant_id, timestamp)` + `(timestamp)` |
| Migration discipline | App runs `alembic upgrade head` at lifespan startup, guarded by `pg_advisory_lock` (M2 — see Section 5 for justification) |

## Architecture

```
                                                     ┌───────────────────────┐
HTTP request ─► route handler (api/routes)           │  Domain (frozen, pure) │
   │                                                  │                       │
   │   builds AnalysisRequest / SummaryRequest        │  AnalysisRequest      │
   │   (tenant_id from auth)                          │  CallContext          │
   ▼                                                  │  LLMCallRecord        │
OrchestratorPort.<op>(req, deadline)                  │  Operation, CallStatus│
   │                                                  └───────────────────────┘
   │   async with self._call_scope(req, operation="<op>"):
   │       ...                                         (sets current_call_context ContextVar)
   ▼
LLMPort.complete(req)                                  (ContextVar carries through await)
   ├─ default wiring:           LiteLLMAdapter        # pure
   └─ when TRACK_COST_IN_DB:    TrackingLLMAdapter ─► UsageRepositoryPort ─► PostgresUsageRepository
                                    │                                            │
                                    ▼ wraps inner adapter                        ▼
                                reads ContextVar, builds                  INSERT INTO llm_calls
                                LLMCallRecord, persists for
                                every attempt (success or
                                failure); never raises on
                                recording failure
```

### Flow per request

1. Route handler authenticates → has `tenant_id`. Builds the domain request.
2. Handler calls `orchestrator.<op>(domain_req, deadline)`.
3. Orchestrator opens an `async with self._call_scope(domain_req, operation=Operation.<OP>):` block. The block sets a `ContextVar[CallContext]` to `CallContext(tenant_id=domain_req.tenant_id, operation=Operation.<OP>)` and resets it on exit.
4. Inside the block, every `await self._llm.complete(req)` call runs through `TrackingLLMAdapter` (when the flag is on).
5. `TrackingLLMAdapter.complete` reads `current_call_context.get()`. **Raises `MissingCallScopeError` if `None`.** Times the inner call. On success or exception, builds an `LLMCallRecord` and persists it. Recording errors are logged; never raised.
6. Analytical reads (`/v1/usage`, `/v1/usage/all`) hit the same `UsageRepositoryPort` to fetch aggregates.

### Removed concept

The existing `TrackingOrchestrator` (decorator at the orchestrator layer that records one summed row per orchestrator call) is **superseded** by `TrackingLLMAdapter`. We do not keep both — having two trackers double-counts and splits the schema. The implementation plan decides whether the orchestrator decorator is deleted outright or refactored into a no-op transitional shim.

### Why ContextVar over an explicit `CallContext` parameter on `LLMPort`

We considered passing `ctx: CallContext` as an explicit kwarg on `LLMPort.complete`. Cleaner type-system story, but:

- Threads through every internal helper in the orchestrator that calls the LLM.
- Doesn't survive `asyncio.gather`-style fan-out unless every spawn site also forwards the kwarg — easy to forget; silent attribution bug.
- Pollutes the `LLMPort` interface for any future adapter (Bedrock, native Anthropic) that doesn't need it.

ContextVar avoids all three:

- One `async with` per public orchestrator method; nothing threaded through helpers.
- `asyncio.create_task` / `gather` propagate ContextVars natively (snapshot-on-spawn).
- `LLMPort.complete` signature stays unchanged.

The "ambient state is bad" concern that initially pointed away from ContextVar applies to *globals across boundaries*. A request-scoped value set at the orchestrator's public-method entry, read in a sibling adapter module, and reset on method exit is functionally equivalent to a function-local widened to one call tree.

The strict-raise policy (`MissingCallScopeError` if scope is unset) eliminates the "silent nulls" failure mode that lenient ContextVar schemes are usually criticized for: a wiring bug becomes a developer-visible exception at first invocation, not a poisoned analytical query weeks later.

## Domain model

Lives in `qfa.domain.models` (and `qfa.domain.errors` for the new exception). All models frozen Pydantic v2 per ADR-001.

### `Operation` and `CallStatus` enums

```python
from enum import StrEnum

class Operation(StrEnum):
    ANALYZE             = "analyze"
    SUMMARIZE           = "summarize"
    SUMMARIZE_AGGREGATE = "summarize_aggregate"
    ASSIGN_CODES        = "assign_codes"

class CallStatus(StrEnum):
    OK    = "ok"
    ERROR = "error"
```

DB columns stay `varchar(64)` / `varchar(16)` — adding `Operation.NEW_THING` is a Python change with no migration.

### `CallContext`

```python
class CallContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant_id: str
    operation: Operation
```

### `LLMCallRecord` (modified)

```python
class LLMCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id:        str
    operation:        Operation              # NEW
    timestamp:        datetime                # UTC, tz-aware
    call_duration_ms: int
    model:            str
    input_tokens:     int                    # 0 on failure with no usage info
    output_tokens:    int                    # 0 on failure with no usage info
    cost_usd:         Decimal                # NEW; 0 on failure with no cost
    status:           CallStatus             # NEW
    error_class:      str | None = None      # NEW; type(exc).__name__ when status=error
```

Invariant: `error_class` is non-`None` iff `status == CallStatus.ERROR` (enforced at DB level via CHECK constraint, see Schema).

### `OperationStats` (new) and `UsageStats` (modified)

```python
class OperationStats(BaseModel):                    # NEW — one row in by_operation
    model_config = ConfigDict(frozen=True)
    operation:           Operation
    total_calls:         int                        # incl. failures
    failed_calls:        int                        # status != ok
    cost_usd:            Decimal                    # status = ok only
    input_tokens_total:  int                        # status = ok only
    output_tokens_total: int                        # status = ok only

class UsageStats(BaseModel):                        # MODIFIED
    model_config = ConfigDict(frozen=True)
    tenant_id:       str | None = None              # None = grand total (superuser only)
    total_calls:     int                            # incl. failures
    failed_calls:    int                            # NEW
    total_cost_usd:  Decimal                        # NEW; status = ok only
    call_duration:   DistributionStats              # status = ok only
    input_tokens:    TokenStats                     # status = ok only
    output_tokens:   TokenStats                     # status = ok only
    by_operation:    tuple[OperationStats, ...]     # NEW; sorted cost_usd desc, ties by operation asc
```

### Errors

```python
# qfa.domain.errors
class MissingCallScopeError(RuntimeError):
    """Raised when an LLM call is recorded without an active CallContext.

    Indicates a wiring bug: the orchestrator forgot to enter a call_scope
    block before calling the LLM. Should never reach a user.
    """
```

### Notes

- `cost_usd: Decimal` is serialized as a **JSON number** in API responses (configured via Pydantic v2 field config). At `numeric(12, 6)` precision, IEEE-754 double has more than enough headroom; clients consuming JSON expect a number.
- `error_class` is a string, not a typed reference. Persisting class names lets the codebase rename exceptions without orphaning historical rows and lets analytical queries filter by error class without an import dance.

## Database schema

Target shape for `llm_calls`. The current table from the prior `fbeeb04c3d36_create_llm_calls_table.py` migration is a subset of this; a new alembic migration moves it to the target shape (additive vs. drop-and-recreate decision deferred to the implementation plan).

```sql
CREATE TABLE llm_calls (
    id                BIGSERIAL       PRIMARY KEY,
    tenant_id         VARCHAR(255)    NOT NULL,
    operation         VARCHAR(64)     NOT NULL,
    timestamp         TIMESTAMPTZ     NOT NULL,
    call_duration_ms  INTEGER         NOT NULL,
    model             VARCHAR(255)    NOT NULL,
    input_tokens      INTEGER         NOT NULL DEFAULT 0,
    output_tokens     INTEGER         NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12, 6)  NOT NULL DEFAULT 0,
    status            VARCHAR(16)     NOT NULL,
    error_class       VARCHAR(128)    NULL,
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT llm_calls_status_known
        CHECK (status IN ('ok', 'error')),
    CONSTRAINT llm_calls_input_tokens_nonneg
        CHECK (input_tokens  >= 0),
    CONSTRAINT llm_calls_output_tokens_nonneg
        CHECK (output_tokens >= 0),
    CONSTRAINT llm_calls_cost_nonneg
        CHECK (cost_usd      >= 0),
    CONSTRAINT llm_calls_duration_nonneg
        CHECK (call_duration_ms >= 0),
    CONSTRAINT llm_calls_error_class_iff_error
        CHECK ((status = 'error') = (error_class IS NOT NULL))
);

CREATE INDEX idx_llm_calls_tenant_timestamp
    ON llm_calls (tenant_id, timestamp);

CREATE INDEX idx_llm_calls_timestamp
    ON llm_calls (timestamp);
```

### Column rationale (highlights)

- **`tenant_id` — no FK.** Tenants live in API-key config (env-loaded `TenantApiKey`), not in a DB table. A FK would force a sync mechanism for an unbounded benefit.
- **`operation VARCHAR(64)`, not `ENUM`.** Adding new operations without a DB migration; DB-level CHECK is unnecessary because the `Operation` StrEnum constrains the writer side.
- **`timestamp TIMESTAMPTZ`** — the wall-clock when the LLM call started. UTC-coerced by asyncpg.
- **`created_at`** — when the row was inserted. Distinct from `timestamp` so a future async/queued recorder can be measured for ingest lag without backfilling.
- **`cost_usd NUMERIC(12, 6)`** — exact decimal; capacity ≈ $999,999.999999 per row.
- **`llm_calls_error_class_iff_error` CHECK** — DB-level guarantee that `error_class` is non-null exactly when `status='error'`. Co-evolutionary with the application invariant.

### Indexes

- `(tenant_id, timestamp)` serves `/v1/usage` (per-tenant + time filter); the by-operation breakdown is a heap re-scan on top of this filter.
- `(timestamp)` serves `/v1/usage/all` and superuser cross-tenant queries.

A composite `(tenant_id, operation, timestamp)` is **not** added in v1; revisit only if measurement shows the by-operation breakdown is slow.

## API contract

### `GET /v1/usage`

**Auth:** `Authorization: Bearer <api_key>` (any valid tenant key).
**Scope:** the authenticated tenant only.

**Query parameters** (all optional):

| name | type | semantics |
|---|---|---|
| `from` | ISO-8601 timestamptz | inclusive lower bound; rejects naive datetimes |
| `to`   | ISO-8601 timestamptz | exclusive upper bound; rejects naive datetimes |

If both supplied, `to` must be strictly greater than `from` — else 422.
Default: no filter (full history).

**Response 200:**
```jsonc
{
  "tenant_id": "tenant-nl",
  "from": "2026-04-01T00:00:00Z",         // echoes effective filter; null if not given
  "to":   "2026-05-01T00:00:00Z",
  "total_calls":   1234,
  "failed_calls":     7,
  "total_cost_usd":  87.421500,
  "call_duration": { "avg": 412.0, "min": 120, "max": 9100, "p5": 180, "p95": 1700 },
  "input_tokens":  { "avg": 7600, "min":  80, "max": 32000, "p5": 200, "p95": 18000, "total": 9_400_000 },
  "output_tokens": { "avg":  493, "min":   4, "max":  6400, "p5":  18, "p95":  1900, "total":   600_000 },
  "by_operation": [
    {"operation": "analyze",   "total_calls": 800, "failed_calls": 5, "cost_usd": 70.105200, "input_tokens_total": 6_800_000, "output_tokens_total": 420_000},
    {"operation": "summarize", "total_calls": 300, "failed_calls": 1, "cost_usd": 12.041100, "input_tokens_total": 1_900_000, "output_tokens_total": 140_000}
    // ...
  ]
}
```

**Empty-window behavior:** valid filter, no matching rows → 200 with all-zero counts and `by_operation: []`. Not 404.

**Errors:**
- 401 — missing/invalid bearer token.
- 422 — `from`/`to` is naive, malformed, or `to <= from`.
- 503 — `TRACK_COST_IN_DB` is off **or** DB is unavailable. Body distinguishes the two via a machine-readable reason code.

### `GET /v1/usage/all`

**Auth:** `Authorization: Bearer <superuser_api_key>` (`is_superuser=true`).
**Scope:** all tenants.

Same query params as `/v1/usage`.

**Response 200:**
```jsonc
{
  "from": "2026-04-01T00:00:00Z",
  "to":   "2026-05-01T00:00:00Z",
  "tenants": [
    { /* one UsageStats object per tenant; same shape as /v1/usage minus the top-level from/to */ },
    // ...
  ],
  "total": {
    "tenant_id": null,                     // sentinel: grand total across tenants
    "total_calls":    12_345,
    "failed_calls":       89,
    "total_cost_usd":  921.401200,
    "call_duration": { /* ... */ },
    "input_tokens":  { /* ... */ },
    "output_tokens": { /* ... */ },
    "by_operation": [
      // grand totals per operation, across all tenants
    ]
  }
}
```

**Errors:**
- 401 — missing/invalid bearer token.
- 403 — token is valid but `is_superuser=false`.
- 422 — same as `/v1/usage`.
- 503 — same as `/v1/usage`.

### Time-filter semantics

- Half-open `[from, to)`. Lets a caller chain consecutive windows without double-counting boundary rows.
- Both fields **must be timezone-aware**; bare `"2026-04-01T00:00:00"` (no tz) is rejected with 422.
- Both fields are converted to UTC before query; the response echoes them in UTC ISO-8601.
- Either may be omitted independently (`from` omitted = beginning of time; `to` omitted = now).

### Sort orders

- `tenants` — alphabetical by `tenant_id`.
- `by_operation` — `cost_usd` desc, ties broken by `operation` asc. (Most expensive operation reads top-down.)

### Out of scope for the contract

- Pagination on `/v1/usage/all.tenants`. (Tenant count is small; YAGNI.)
- Sort/order query params. (Server-side stable ordering covers the analytical questions.)
- A "top N operations" endpoint. (Subsumed by `by_operation` at small operation cardinality.)
- A `?status=` filter. (Failure counts are first-class; deeper "which calls failed" is a future concern.)
- HTTP caching headers / ETags.

## Configuration & feature flag

### Environment variables

| Variable | Type | Default | When read | Notes |
|---|---|---|---|---|
| `TRACK_COST_IN_DB` | `bool` | `false` | startup | Master switch. |
| `DATABASE_URL` | `str` | unset | startup, only when flag is on | Async-driver URL: `postgresql+asyncpg://user:pass@host:5432/db`. Treated as a secret. |

**Cross-validation in `qfa.settings.Settings`** (`model_validator(mode="after")`):

- `TRACK_COST_IN_DB=true` ⇒ `DATABASE_URL` must be present and parseable.
- `TRACK_COST_IN_DB=false` ⇒ `DATABASE_URL` ignored (allowed but unused).
- A missing `DATABASE_URL` while the flag is on is a **startup error**.

### Wiring at startup (`main.py` lifespan)

1. Load `Settings`.
2. **Flag off:** construct `LiteLLMAdapter` bare; `app.state.usage_repo = None`; no DB import paths execute. Done.
3. **Flag on:**
   1. Create the async SQLAlchemy engine + sessionmaker against `DATABASE_URL`.
   2. **Acquire `pg_advisory_lock(LLM_CALLS_MIGRATION_LOCK_KEY)`**, run `alembic upgrade head`, release the lock. Concurrent replicas race for the lock; non-winners wait for the schema to match `head`. Lock is session-scoped, so a crashed migrator releases on connection close.
   3. Probe connectivity / schema via `SELECT 1`; fail fast if unavailable post-migration.
   4. Construct `PostgresUsageRepository(sessionmaker)` and assign to `app.state.usage_repo`.
   5. Wrap: `llm = TrackingLLMAdapter(inner=LiteLLMAdapter(...), usage_repo=app.state.usage_repo)`.
   6. Construct orchestrator with the wrapped `llm`.
4. On shutdown: `engine.dispose()`.

### Connection pool

```python
create_async_engine(
    DATABASE_URL,
    pool_size       = 5,
    max_overflow    = 10,
    pool_pre_ping   = True,    # survives idle disconnects (Azure managed Postgres)
    pool_recycle    = 1800,    # 30 min; cuts stale conns before broker-side timeout
)
```

### Migration discipline (M2 — startup, advisory-lock-guarded)

Operational constraint: prod Postgres lives in a private Azure VNet with no externally-reachable endpoint. CI runners and developer laptops cannot run `alembic upgrade head` directly against prod.

We considered:

- **M1: dedicated migration job** (Azure Container App Job triggered by CI) — clean separation, but ~30–80 lines of new Terraform and a CI step for a feature that could be self-contained.
- **M2: app-startup migration with `pg_advisory_lock`** — chosen.
- **M3: manual op via Azure Bastion** — rejected as not auditable / not scalable.

**Why M2 for this project:**

1. The `pg_advisory_lock` resolves the multi-replica race that motivates the Twelve-Factor "release ≠ run" rule at the DB level.
2. Single web app, infrequent migrations, small team — operational simplicity beats infra purity.
3. Dev (`make migrate` against docker-compose Postgres) and prod (lifespan startup) execute the same migration code path.

**Discipline this requires:**

- Migrations are **backwards-compatible** so rolling deploys (old replica + new replica) coexist for the duration of a deploy.
- Readiness probes are tuned (timeout ≥ 5 min) to accommodate longer migrations.
- The advisory-lock dance is **load-bearing** — must not be removed without a designed replacement.

**Switch to M1 if:**

- Migration runtime grows past a minute or two (startup probes get unhappy).
- Multiple environments need different deploy cadences with clean per-env "this env is at migration N" signal.
- Other services share the DB and migrations need cross-service coordination.

### Local development

`docker-compose.yml` adds a Postgres service (named volume for persistence, healthcheck for ordering). `Makefile` gains:

| target | does |
|---|---|
| `make db-up` | `docker compose up -d postgres` |
| `make db-down` | `docker compose down` (data preserved) |
| `make db-reset` | down + remove volume + up (destructive) |
| `make migrate` | `uv run alembic upgrade head` |

`make test` and `make lint` are **not** changed to require a running DB. Tests that need a DB pull in their own fixture (Section: Testing).

### Operational expectations

- **DB down at write time.** Tracking adapter logs the recording failure (with structured fields) and **does not raise** — the LLM response still flows back to the user. Only analytics has a hole; the hole is observable in logs.
- **DB down at read time.** `/v1/usage*` returns 503 with a body indicating "usage backend unavailable", distinct from "feature disabled" via a machine-readable reason code.
- **Backpressure.** No queue/buffer for failed writes — they are dropped after logging. A buffered/outbox approach is a future improvement (see Out of scope).

## Testing

Three tiers, each with a clear fault domain.

### Tier 1 — Unit tests (no I/O)

| What | How | Lives in |
|---|---|---|
| Domain model validation (`Operation`, `CallStatus`, `CallContext`, `LLMCallRecord`, `OperationStats`, `UsageStats`) | Pydantic constructor + field-error assertions | `tests/domain/` |
| `TrackingLLMAdapter` — success path, failure path, cost from response, all-attempts-recorded | `FakeLLMPort` inner + `FakeUsageRepository` + `call_scope` helper | `tests/adapters/test_tracking_llm.py` |
| `TrackingLLMAdapter` — `MissingCallScopeError` raised when scope is unset | `with pytest.raises(MissingCallScopeError):` | same file |
| `LiteLLMAdapter` (bare) | Existing tests; **no changes** | `tests/services/test_llm_client.py` |
| Orchestrator — verifies each public method enters `call_scope` with the correct operation, propagates to inner | `FakeLLMPort` records both the request and the captured `current_call_context.get()` per call | `tests/services/test_orchestrator.py` |

#### Shared helper (`tests/conftest.py`)

```python
@asynccontextmanager
async def call_scope(tenant_id="t1", operation=Operation.ANALYZE):
    token = current_call_context.set(CallContext(tenant_id=tenant_id, operation=operation))
    try:
        yield
    finally:
        current_call_context.reset(token)
```

#### Test doubles (`tests/fakes/`)

- `FakeLLMPort` — implements `LLMPort`; `queue_response`, `queue_failure`; records every call. Empty queue raises `AssertionError` on call (loud failure for unexpected invocations).
- `FakeUsageRepository` — implements `UsageRepositoryPort`; list-backed `records: list[LLMCallRecord]`.

These are **fakes, not mocks**. Type-checked, hexagonally consistent (just another adapter), centralized so signature changes update one file.

### Tier 2 — Integration tests (real Postgres, no HTTP)

`@pytest.mark.integration`, opt-in. Default `make test` runs Tier 1 only. New `make test-integration` runs Tier 1 + 2.

| What | How |
|---|---|
| `PostgresUsageRepository.record_call` round-trips `LLMCallRecord` fields, `cost_usd` as `Decimal` | Insert + read-back |
| DB-level `error_class iff status='error'` constraint | Assert `IntegrityError` on mismatched rows |
| Time filter half-open semantics | Seed boundary rows; assert `from` row included, `to` row excluded |
| α policy: cost/tokens exclude failed calls | Mixed `ok`+`error` rows; assert `total_cost_usd` matches only the `ok` sum |
| Empty window | Assert 200-shaped zeros + empty `by_operation` |
| `by_operation` sort order | Seed deterministic costs; assert `cost_usd` desc, ties by `operation` asc |
| Index usage | `EXPLAIN` assertion: canonical queries pick `idx_llm_calls_tenant_timestamp` / `idx_llm_calls_timestamp` |

**DB lifecycle:** one ephemeral Postgres container per test session (docker-compose service started by a session-scoped fixture). `alembic upgrade head` once at session start. **Per-test isolation via SAVEPOINT-rollback** (SQLAlchemy 2.x `nested_transaction`). No `TRUNCATE` between tests; no order dependence.

### Tier 3 — End-to-end API tests

`@pytest.mark.e2e`. Full FastAPI stack via `httpx.AsyncClient`, real Postgres, real lifespan startup. LiteLLM is faked at the **HTTP transport** layer (via `respx`) so the **real** `LiteLLMAdapter` and **real** `TrackingLLMAdapter` are exercised end-to-end — including `response_cost` extraction and exception classes.

| What | How |
|---|---|
| `GET /v1/usage` happy path | Seed rows, hit endpoint, assert response shape & numbers |
| `GET /v1/usage?from=...&to=...` time-filter inclusivity / exclusivity | Boundary seed; assert `from` row included, `to` row excluded |
| `GET /v1/usage` empty window | 200 with zeros, not 404 |
| `GET /v1/usage` naive datetime / `to <= from` | Both 422 |
| `GET /v1/usage` flag off | 503; no DB session opened |
| `GET /v1/usage/all` non-superuser key | 403 |
| `GET /v1/usage/all` superuser key | tenants list + `total` row with `tenant_id: null` |
| `POST /v1/analyze` records a row with `operation=analyze` | `respx`-faked OpenAI; query repo directly to assert the row |
| `POST /v1/analyze` with LLM failure | `respx` raises; assert `status=error`, `error_class` populated, `cost_usd=0` |
| `POST /v1/assign_codes` records **multiple rows per call** | `respx` responds N times; assert N rows, all `operation=assign_codes` |

### Migration tests

| What | How |
|---|---|
| `alembic upgrade head` on empty DB | Tier 2 fixture |
| `downgrade base` then `upgrade head` is idempotent | Same DB, run sequence |
| Advisory-lock dance: two concurrent migrators don't corrupt the version table | Spawn two `asyncio.create_task` migrators; assert one wins, other waits, both end at `head` |

Migration tests use a fresh DB (or `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` between tests), not the SAVEPOINT fixture.

### Out of scope for testing

- Load testing / benchmarks for analytical endpoints.
- Property-based / fuzz testing of the time-filter parsing.
- Cross-database compatibility (only Postgres is supported).

## Out of scope (deferred follow-ups)

These are intentional non-goals for this iteration. Listed so they don't quietly creep in.

- **Sub-operation labels** (option C from brainstorming) — `assign_codes.pick_level_1`, `summarize_aggregate.judge`. Adds a second column; can be introduced as a backwards-compatible additive change later.
- **Pre-aggregated rollup tables / materialized views** for analytical queries.
- **Table partitioning** (by month / by tenant).
- **Retention / archival policy** (auto-delete after N months).
- **Foreign-key relationship** to a future `tenants` table.
- **Outbox pattern** for tracking writes (queue failed writes for retry instead of dropping).
- **Dedicated `Migration` job (M1)** — only if/when M2 hits its limits.
- **Pagination, sort/order params, top-N, status filter** on the analytical endpoints.
- **HTTP caching headers / ETags.**
- **Distribution stats per operation** (option iii from brainstorming) — added later as `/v1/usage/by-operation/distributions` if it earns its keep.
- **Generic `?group_by=` query language** (option R from brainstorming).

## Open follow-ups (intentionally undecided here, decided in implementation plan)

- Migration strategy for the existing `llm_calls` table: additive migration (preserve historical rows, default `operation` to a sentinel like `"unknown"`) vs. drop-and-recreate (clean slate, since data is only weeks old). Both are viable; the choice is a small operational call best made when the implementer surveys current row count.
- Disposition of the existing `TrackingOrchestrator`: delete vs. transitional shim. Either works; pick what minimizes diff risk.

## References

- Issue: rodekruis/qualitative-feedback-analysis#5
- Prior planning thread: comment on issue #5 (token-only scope, superseded by this spec).
- Related ADR: ADR-001 (frozen Pydantic + tuples).
- Related design: `2026-04-10-litellm-model-switching-design.md` (introduced LiteLLM and `cost` in `LLMResponse`).
