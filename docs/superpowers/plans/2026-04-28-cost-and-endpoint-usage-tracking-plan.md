# Cost and Endpoint Usage Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track every LLM call attempt (success and failure) per tenant per orchestrator operation with cost, persist to Postgres behind a feature flag, and expose aggregated stats with time-filter via `/v1/usage` and `/v1/usage/all`.

**Architecture:** Per-attempt recording at the LLM-port layer via a `TrackingLLMAdapter` decorator. Tenant + operation propagate through a request-scoped `ContextVar` set inside an `async with self._call_scope(...)` block at each public orchestrator method entry. The existing orchestrator-layer `TrackingOrchestrator` is deleted (one-row-per-orchestrator-call is superseded by one-row-per-attempt). Schema extended additively (preserving historical rows backfilled with `Operation.UNKNOWN`).

**Tech Stack:** Python 3.12/3.13, FastAPI, SQLAlchemy 2.x async, asyncpg, Alembic, Pydantic v2, LiteLLM, pytest, ruff, ty.

**Spec:** `docs/superpowers/specs/2026-04-28-cost-and-endpoint-usage-tracking-design.md`

**Engagement model:** Default `make test` (and CI's `pytest`) runs **unit tests only**. Postgres-dependent integration tests are gated by a `@pytest.mark.integration` marker and excluded by default; run with `pytest -m integration`. End-to-end tests with `respx`-faked LiteLLM are gated by `@pytest.mark.e2e`. CI does NOT need Docker; manual `make test-integration` exists for local verification.

**Existing code mapping (spec ↔ repo):**
| Spec name | Existing class/file |
|---|---|
| `LiteLLMAdapter` | `qfa.services.llm_client.LiteLLMClient` |
| `PostgresUsageRepository` | `qfa.adapters.db.SqlAlchemyUsageRepository` |
| `TrackingLLMAdapter` | NEW — `qfa.adapters.tracking_llm.TrackingLLMAdapter` |
| `current_call_context` ContextVar | NEW — `qfa.adapters.call_context` |

**LLMPort signature (existing, kept as-is):** `complete(system_message, user_message, timeout, tenant_id)` — the spec's `complete(req)` is idealized; the wrapper just preserves the existing positional signature.

---

## Task 1: Add `Operation`, `CallStatus`, `CallContext`, and `MissingCallScopeError` to the domain

**Files:**
- Modify: `src/qfa/domain/models.py`
- Modify: `src/qfa/domain/errors.py`
- Modify: `src/qfa/domain/__init__.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing test for the new enums and CallContext**

Append to `tests/domain/test_models.py`:

```python
from qfa.domain.models import CallContext, CallStatus, Operation


def test_operation_enum_values():
    assert Operation.ANALYZE == "analyze"
    assert Operation.SUMMARIZE == "summarize"
    assert Operation.SUMMARIZE_AGGREGATE == "summarize_aggregate"
    assert Operation.ASSIGN_CODES == "assign_codes"
    assert Operation.UNKNOWN == "unknown"


def test_call_status_enum_values():
    assert CallStatus.OK == "ok"
    assert CallStatus.ERROR == "error"


def test_call_context_is_frozen():
    ctx = CallContext(tenant_id="t1", operation=Operation.ANALYZE)
    with pytest.raises(Exception):
        ctx.tenant_id = "t2"  # type: ignore[misc]
```

(Add `import pytest` if not already imported in the file.)

- [ ] **Step 2: Run test — expect ImportError**

```
uv run pytest tests/domain/test_models.py -k "operation_enum or call_status or call_context" -v
```

Expected: ImportError on `Operation`/`CallStatus`/`CallContext`.

- [ ] **Step 3: Add the enums and `CallContext` to `qfa.domain.models`**

In `src/qfa/domain/models.py`, add at the top after existing imports:

```python
from enum import StrEnum
```

Add these classes near the other domain enums (place them right above `LLMCallRecord`):

```python
class Operation(StrEnum):
    """Orchestrator operations that produce LLM calls.

    Stored as strings in the database; new members can be added without
    a DB migration. ``UNKNOWN`` exists as a sentinel for backfilled rows
    from before per-operation tracking was introduced.
    """

    ANALYZE = "analyze"
    SUMMARIZE = "summarize"
    SUMMARIZE_AGGREGATE = "summarize_aggregate"
    ASSIGN_CODES = "assign_codes"
    UNKNOWN = "unknown"


class CallStatus(StrEnum):
    """Outcome of a single LLM call attempt."""

    OK = "ok"
    ERROR = "error"


class CallContext(BaseModel):
    """Per-call context carried via ContextVar from orchestrator to tracker.

    Attributes
    ----------
    tenant_id : str
        Tenant making the call.
    operation : Operation
        Public orchestrator operation that issued the call.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    operation: Operation
```

- [ ] **Step 4: Add `MissingCallScopeError` to `qfa.domain.errors`**

Append to `src/qfa/domain/errors.py`:

```python
# --- Tracking errors ---


class MissingCallScopeError(RuntimeError):
    """Raised when an LLM call is recorded without an active CallContext.

    Indicates a wiring bug: the orchestrator forgot to enter a ``call_scope``
    block before calling the LLM. Should never reach a user.
    """
```

- [ ] **Step 5: Re-export from `qfa.domain` package**

Open `src/qfa/domain/__init__.py` and ensure it exports the new symbols. If the file currently re-exports model names, add `Operation`, `CallStatus`, `CallContext` to that list. If it only re-exports a few, add at minimum:

```python
from qfa.domain.models import (
    CallContext,
    CallStatus,
    FeedbackItem,
    Operation,
)
```

(Do not remove existing exports.)

- [ ] **Step 6: Run tests — expect pass**

```
uv run pytest tests/domain/test_models.py -k "operation_enum or call_status or call_context" -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```
git add src/qfa/domain/models.py src/qfa/domain/errors.py src/qfa/domain/__init__.py tests/domain/test_models.py
git commit -m "feat(domain): add Operation, CallStatus, CallContext and MissingCallScopeError"
```

---

## Task 2: Extend `LLMCallRecord` with `operation`, `cost_usd`, `status`, `error_class`

**Files:**
- Modify: `src/qfa/domain/models.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing tests for the new fields and the error-class invariant**

Append to `tests/domain/test_models.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError

from qfa.domain.models import LLMCallRecord


def _now() -> datetime:
    return datetime.now(UTC)


def test_llm_call_record_ok_status():
    rec = LLMCallRecord(
        tenant_id="t1",
        operation=Operation.ANALYZE,
        timestamp=_now(),
        call_duration_ms=100,
        model="gpt-4",
        input_tokens=10,
        output_tokens=20,
        cost_usd=Decimal("0.0001"),
        status=CallStatus.OK,
    )
    assert rec.status == CallStatus.OK
    assert rec.error_class is None


def test_llm_call_record_error_requires_error_class():
    with pytest.raises(ValidationError):
        LLMCallRecord(
            tenant_id="t1",
            operation=Operation.ANALYZE,
            timestamp=_now(),
            call_duration_ms=100,
            model="gpt-4",
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status=CallStatus.ERROR,
            error_class=None,
        )


def test_llm_call_record_ok_rejects_error_class():
    with pytest.raises(ValidationError):
        LLMCallRecord(
            tenant_id="t1",
            operation=Operation.ANALYZE,
            timestamp=_now(),
            call_duration_ms=100,
            model="gpt-4",
            input_tokens=10,
            output_tokens=20,
            cost_usd=Decimal("0.0001"),
            status=CallStatus.OK,
            error_class="LLMTimeoutError",
        )
```

- [ ] **Step 2: Run tests — expect failures (extra fields rejected, no enforcement of invariant)**

```
uv run pytest tests/domain/test_models.py -k "llm_call_record" -v
```

Expected: FAIL — current `LLMCallRecord` rejects unknown fields and has no invariant.

- [ ] **Step 3: Replace the `LLMCallRecord` class in `src/qfa/domain/models.py`**

Replace the existing `LLMCallRecord` class with:

```python
class LLMCallRecord(BaseModel):
    """A single recorded LLM call attempt for usage and cost tracking.

    Recorded once per LLM-call attempt — success or failure. ``cost_usd``
    and token totals are populated only for successful attempts; failures
    record zeros plus ``error_class``.

    Attributes
    ----------
    tenant_id : str
        Tenant that made the call.
    operation : Operation
        Public orchestrator operation that issued the call.
    timestamp : datetime
        UTC wall-clock when the call started.
    call_duration_ms : int
        Wall-clock duration of the call in milliseconds.
    model : str
        The LLM model used.
    input_tokens : int
        Number of input (prompt) tokens; 0 on failure.
    output_tokens : int
        Number of output (completion) tokens; 0 on failure.
    cost_usd : Decimal
        Estimated cost in USD; 0 on failure.
    status : CallStatus
        Outcome of the attempt.
    error_class : str | None
        ``type(exc).__name__`` when ``status == CallStatus.ERROR``;
        ``None`` otherwise. Enforced by ``model_validator``.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    operation: Operation
    timestamp: datetime
    call_duration_ms: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    status: CallStatus
    error_class: str | None = None

    @model_validator(mode="after")
    def _error_class_iff_error(self) -> "LLMCallRecord":
        if self.status == CallStatus.ERROR and self.error_class is None:
            raise ValueError("error_class is required when status='error'")
        if self.status == CallStatus.OK and self.error_class is not None:
            raise ValueError("error_class must be None when status='ok'")
        return self
```

Also at the top of the file, ensure these imports exist:

```python
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
```

- [ ] **Step 4: Run tests — expect pass**

```
uv run pytest tests/domain/test_models.py -k "llm_call_record" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/qfa/domain/models.py tests/domain/test_models.py
git commit -m "feat(domain): extend LLMCallRecord with operation, cost_usd, status, error_class"
```

---

## Task 3: Extend `UsageStats` with cost/failure/by-operation; add `OperationStats`

**Files:**
- Modify: `src/qfa/domain/models.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/domain/test_models.py`:

```python
from qfa.domain.models import OperationStats


def test_operation_stats_construction():
    stats = OperationStats(
        operation=Operation.ANALYZE,
        total_calls=10,
        failed_calls=1,
        cost_usd=Decimal("0.5"),
        input_tokens_total=1000,
        output_tokens_total=200,
    )
    assert stats.operation == Operation.ANALYZE
    assert stats.failed_calls == 1


def test_usage_stats_has_failed_calls_total_cost_and_by_operation():
    stats = UsageStats(
        tenant_id="t1",
        total_calls=10,
        failed_calls=1,
        total_cost_usd=Decimal("0.5"),
        call_duration=DistributionStats(avg=1, min=0, max=2, p5=0, p95=2),
        input_tokens=TokenStats(avg=1, min=0, max=2, p5=0, p95=2, total=10),
        output_tokens=TokenStats(avg=1, min=0, max=2, p5=0, p95=2, total=10),
        by_operation=(
            OperationStats(
                operation=Operation.ANALYZE,
                total_calls=10,
                failed_calls=1,
                cost_usd=Decimal("0.5"),
                input_tokens_total=1000,
                output_tokens_total=200,
            ),
        ),
    )
    assert stats.total_cost_usd == Decimal("0.5")
    assert stats.failed_calls == 1
    assert len(stats.by_operation) == 1
```

(Ensure `UsageStats`, `DistributionStats`, `TokenStats` are imported in the test module.)

- [ ] **Step 2: Run — expect ImportError + ValidationError**

```
uv run pytest tests/domain/test_models.py -k "operation_stats or by_operation or failed_calls" -v
```

Expected: FAIL.

- [ ] **Step 3: Add `OperationStats` and update `UsageStats` in `src/qfa/domain/models.py`**

Add `OperationStats` directly above `UsageStats`:

```python
class OperationStats(BaseModel):
    """Per-operation aggregated stats for a tenant or grand total.

    Attributes
    ----------
    operation : Operation
        The orchestrator operation.
    total_calls : int
        Total attempts (successful + failed).
    failed_calls : int
        Attempts with ``status='error'``.
    cost_usd : Decimal
        Sum of ``cost_usd`` for successful attempts only.
    input_tokens_total : int
        Sum of ``input_tokens`` for successful attempts only.
    output_tokens_total : int
        Sum of ``output_tokens`` for successful attempts only.
    """

    model_config = ConfigDict(frozen=True)

    operation: Operation
    total_calls: int
    failed_calls: int
    cost_usd: Decimal
    input_tokens_total: int
    output_tokens_total: int
```

Replace `UsageStats` with:

```python
class UsageStats(BaseModel):
    """Aggregated usage statistics for a tenant or grand total.

    The token and duration distributions and ``total_cost_usd`` are scoped
    to ``status='ok'`` rows. ``total_calls`` and ``failed_calls`` count all
    attempts including failures (policy α).

    Attributes
    ----------
    tenant_id : str | None
        Tenant identifier, or None for grand total.
    total_calls : int
        Total attempts (successful + failed).
    failed_calls : int
        Attempts with ``status='error'``.
    total_cost_usd : Decimal
        Sum of cost over successful attempts only.
    call_duration : DistributionStats
        Call duration distribution in ms (successful attempts only).
    input_tokens : TokenStats
        Input token distribution (successful attempts only).
    output_tokens : TokenStats
        Output token distribution (successful attempts only).
    by_operation : tuple[OperationStats, ...]
        Per-operation breakdown, sorted ``cost_usd`` desc with ties broken by
        ``operation`` asc.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    total_calls: int
    failed_calls: int
    total_cost_usd: Decimal
    call_duration: DistributionStats
    input_tokens: TokenStats
    output_tokens: TokenStats
    by_operation: tuple[OperationStats, ...] = ()
```

- [ ] **Step 4: Run — expect pass**

```
uv run pytest tests/domain/test_models.py -v
```

Expected: PASS for new tests; pre-existing tests that constructed `UsageStats` without `failed_calls`/`total_cost_usd` will now fail. That's fine — those tests are updated in later tasks.

- [ ] **Step 5: Commit**

```
git add src/qfa/domain/models.py tests/domain/test_models.py
git commit -m "feat(domain): add OperationStats and extend UsageStats with cost/failure/by_operation"
```

---

## Task 4: Add the `current_call_context` ContextVar module

**Files:**
- Create: `src/qfa/adapters/call_context.py`
- Test: `tests/adapters/test_call_context.py`

- [ ] **Step 1: Write failing test**

Create `tests/adapters/test_call_context.py`:

```python
"""Tests for the request-scoped call-context ContextVar."""

import asyncio

import pytest

from qfa.adapters.call_context import call_scope, current_call_context
from qfa.domain.models import Operation


pytestmark = pytest.mark.asyncio


async def test_current_call_context_is_none_outside_scope():
    assert current_call_context.get() is None


async def test_call_scope_sets_and_resets():
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        ctx = current_call_context.get()
        assert ctx is not None
        assert ctx.tenant_id == "t1"
        assert ctx.operation == Operation.ANALYZE
    assert current_call_context.get() is None


async def test_call_scope_propagates_through_create_task():
    captured: list = []

    async def reader() -> None:
        captured.append(current_call_context.get())

    async with call_scope(tenant_id="t1", operation=Operation.SUMMARIZE):
        await asyncio.create_task(reader())

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].tenant_id == "t1"
    assert captured[0].operation == Operation.SUMMARIZE
```

- [ ] **Step 2: Run — expect ImportError**

```
uv run pytest tests/adapters/test_call_context.py -v
```

Expected: FAIL.

- [ ] **Step 3: Create the module**

Create `src/qfa/adapters/call_context.py`:

```python
"""Request-scoped ContextVar carrying tenant + operation to tracking adapters.

The orchestrator enters ``call_scope(...)`` at each public-method entry; the
``TrackingLLMAdapter`` reads ``current_call_context.get()`` at LLM-call time.
``asyncio`` propagates ContextVars across ``create_task`` / ``gather`` via
snapshot-on-spawn.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from qfa.domain.models import CallContext, Operation

current_call_context: ContextVar[CallContext | None] = ContextVar(
    "current_call_context",
    default=None,
)


@asynccontextmanager
async def call_scope(
    tenant_id: str,
    operation: Operation,
) -> AsyncIterator[CallContext]:
    """Set ``current_call_context`` for the duration of the block.

    Parameters
    ----------
    tenant_id : str
        Tenant making the call.
    operation : Operation
        Public orchestrator operation issuing the call.

    Yields
    ------
    CallContext
        The context that was set.
    """
    ctx = CallContext(tenant_id=tenant_id, operation=operation)
    token = current_call_context.set(ctx)
    try:
        yield ctx
    finally:
        current_call_context.reset(token)
```

- [ ] **Step 4: Run — expect pass**

```
uv run pytest tests/adapters/test_call_context.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/qfa/adapters/call_context.py tests/adapters/test_call_context.py
git commit -m "feat(adapters): add current_call_context ContextVar with call_scope helper"
```

---

## Task 5: Update `UsageRepositoryPort` with time-filter parameters

**Files:**
- Modify: `src/qfa/domain/ports.py`

- [ ] **Step 1: Update the port signatures**

In `src/qfa/domain/ports.py`, replace the `UsageRepositoryPort` class with:

```python
class UsageRepositoryPort(Protocol):
    """Port for recording and querying LLM usage data."""

    async def record_call(self, record: LLMCallRecord) -> None:
        """Record a single LLM call attempt.

        Parameters
        ----------
        record : LLMCallRecord
            The call record to persist.
        """
        ...

    async def get_usage_stats(
        self,
        tenant_id: str,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> UsageStats | None:
        """Get aggregated usage stats for a single tenant.

        Parameters
        ----------
        tenant_id : str
            The tenant to query.
        from_ : datetime | None
            Inclusive lower bound (UTC tz-aware), or None.
        to : datetime | None
            Exclusive upper bound (UTC tz-aware), or None.

        Returns
        -------
        UsageStats | None
            Stats for the tenant, or None if no calls in window.
        """
        ...

    async def get_all_usage_stats(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[UsageStats]:
        """Get per-tenant stats plus a grand total entry (tenant_id=None).

        Parameters
        ----------
        from_ : datetime | None
            Inclusive lower bound (UTC tz-aware), or None.
        to : datetime | None
            Exclusive upper bound (UTC tz-aware), or None.

        Returns
        -------
        list[UsageStats]
            Per-tenant stats followed by a grand total entry.
        """
        ...
```

- [ ] **Step 2: Run lint to verify**

```
uv run ty check
```

Expected: any pre-existing errors, but no new ones from this file (the implementation will catch up in later tasks). The `SqlAlchemyUsageRepository` will fail the LSP check until Task 8.

- [ ] **Step 3: Commit**

```
git add src/qfa/domain/ports.py
git commit -m "feat(domain): add time-filter params to UsageRepositoryPort"
```

---

## Task 6: Implement `TrackingLLMAdapter`

**Files:**
- Create: `src/qfa/adapters/tracking_llm.py`
- Test: `tests/adapters/test_tracking_llm.py`

- [ ] **Step 1: Write failing tests**

Create `tests/adapters/test_tracking_llm.py`:

```python
"""Tests for TrackingLLMAdapter."""

from decimal import Decimal

import pytest

from qfa.adapters.call_context import call_scope
from qfa.adapters.tracking_llm import TrackingLLMAdapter
from qfa.domain.errors import LLMError, MissingCallScopeError
from qfa.domain.models import (
    CallStatus,
    LLMCallRecord,
    LLMResponse,
    Operation,
)


pytestmark = pytest.mark.asyncio


class FakeLLMPort:
    """Test double for LLMPort. Returns a queued response or raises."""

    def __init__(self) -> None:
        self._next_response: LLMResponse | None = None
        self._next_error: Exception | None = None
        self.calls: list[tuple] = []

    def queue_response(self, response: LLMResponse) -> None:
        self._next_response = response
        self._next_error = None

    def queue_failure(self, exc: Exception) -> None:
        self._next_error = exc
        self._next_response = None

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ) -> LLMResponse:
        self.calls.append((system_message, user_message, timeout, tenant_id))
        if self._next_error is not None:
            raise self._next_error
        assert self._next_response is not None, "queue_response not called"
        return self._next_response


class FakeUsageRepository:
    """Test double for UsageRepositoryPort.record_call."""

    def __init__(self) -> None:
        self.records: list[LLMCallRecord] = []
        self.fail: bool = False

    async def record_call(self, record: LLMCallRecord) -> None:
        if self.fail:
            raise RuntimeError("DB down")
        self.records.append(record)

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        raise NotImplementedError

    async def get_all_usage_stats(self, from_=None, to=None):
        raise NotImplementedError


def _ok_response() -> LLMResponse:
    return LLMResponse(
        text="hello",
        model="gpt-4-test",
        prompt_tokens=10,
        completion_tokens=20,
        cost=0.0001,
    )


async def test_records_successful_call_with_operation_and_cost():
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        result = await adapter.complete(
            system_message="sys",
            user_message="usr",
            timeout=10.0,
            tenant_id="t1",
        )

    assert result.text == "hello"
    assert len(repo.records) == 1
    rec = repo.records[0]
    assert rec.tenant_id == "t1"
    assert rec.operation == Operation.ANALYZE
    assert rec.status == CallStatus.OK
    assert rec.model == "gpt-4-test"
    assert rec.input_tokens == 10
    assert rec.output_tokens == 20
    assert rec.cost_usd == Decimal("0.0001")
    assert rec.error_class is None
    assert rec.call_duration_ms >= 0


async def test_records_failed_call_with_error_class():
    inner = FakeLLMPort()
    inner.queue_failure(LLMError("boom"))
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.SUMMARIZE):
        with pytest.raises(LLMError):
            await adapter.complete(
                system_message="sys",
                user_message="usr",
                timeout=10.0,
                tenant_id="t1",
            )

    assert len(repo.records) == 1
    rec = repo.records[0]
    assert rec.status == CallStatus.ERROR
    assert rec.error_class == "LLMError"
    assert rec.cost_usd == Decimal("0")
    assert rec.input_tokens == 0
    assert rec.output_tokens == 0
    assert rec.operation == Operation.SUMMARIZE


async def test_raises_when_call_scope_unset():
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    with pytest.raises(MissingCallScopeError):
        await adapter.complete(
            system_message="sys",
            user_message="usr",
            timeout=10.0,
            tenant_id="t1",
        )

    # Inner LLM must NOT be called when the scope is missing.
    assert inner.calls == []


async def test_recording_failure_does_not_break_completion():
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    repo = FakeUsageRepository()
    repo.fail = True
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        result = await adapter.complete(
            system_message="sys",
            user_message="usr",
            timeout=10.0,
            tenant_id="t1",
        )

    assert result.text == "hello"


async def test_recording_failure_during_error_path_still_propagates_original():
    inner = FakeLLMPort()
    inner.queue_failure(LLMError("upstream"))
    repo = FakeUsageRepository()
    repo.fail = True
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        with pytest.raises(LLMError, match="upstream"):
            await adapter.complete(
                system_message="sys",
                user_message="usr",
                timeout=10.0,
                tenant_id="t1",
            )
```

- [ ] **Step 2: Run — expect ImportError**

```
uv run pytest tests/adapters/test_tracking_llm.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement the adapter**

Create `src/qfa/adapters/tracking_llm.py`:

```python
"""LLM port decorator that records every call attempt."""

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from qfa.adapters.call_context import current_call_context
from qfa.domain.errors import MissingCallScopeError
from qfa.domain.models import CallStatus, LLMCallRecord, LLMResponse
from qfa.domain.ports import LLMPort, UsageRepositoryPort

logger = logging.getLogger(__name__)


class TrackingLLMAdapter(LLMPort):
    """Decorator over an inner ``LLMPort`` that records every call attempt.

    Reads tenant + operation from ``current_call_context``. Persists one
    ``LLMCallRecord`` per attempt — success or failure. Recording errors
    are logged but never raised.

    Parameters
    ----------
    inner : LLMPort
        The wrapped LLM adapter.
    usage_repo : UsageRepositoryPort
        Repository used to persist call records.
    """

    def __init__(self, inner: LLMPort, usage_repo: UsageRepositoryPort) -> None:
        self._inner = inner
        self._usage_repo = usage_repo

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ) -> LLMResponse:
        """Run the inner ``complete`` and record the attempt.

        Raises
        ------
        MissingCallScopeError
            When ``current_call_context`` is unset — indicates a wiring bug.
        """
        ctx = current_call_context.get()
        if ctx is None:
            raise MissingCallScopeError(
                "TrackingLLMAdapter.complete called outside an active call_scope; "
                "the orchestrator must enter call_scope(...) at each public-method entry."
            )

        started_at = datetime.now(UTC)
        start_monotonic = time.monotonic()

        try:
            response = await self._inner.complete(
                system_message=system_message,
                user_message=user_message,
                timeout=timeout,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_monotonic) * 1000)
            await self._record_safely(
                LLMCallRecord(
                    tenant_id=ctx.tenant_id,
                    operation=ctx.operation,
                    timestamp=started_at,
                    call_duration_ms=duration_ms,
                    model="",
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal("0"),
                    status=CallStatus.ERROR,
                    error_class=type(exc).__name__,
                )
            )
            raise

        duration_ms = int((time.monotonic() - start_monotonic) * 1000)
        await self._record_safely(
            LLMCallRecord(
                tenant_id=ctx.tenant_id,
                operation=ctx.operation,
                timestamp=started_at,
                call_duration_ms=duration_ms,
                model=response.model,
                input_tokens=response.prompt_tokens,
                output_tokens=response.completion_tokens,
                cost_usd=_to_decimal(response.cost),
                status=CallStatus.OK,
                error_class=None,
            )
        )
        return response

    async def _record_safely(self, record: LLMCallRecord) -> None:
        try:
            await self._usage_repo.record_call(record)
        except Exception:
            logger.exception(
                "Failed to record LLM call for tenant=%s operation=%s",
                record.tenant_id,
                record.operation,
            )


def _to_decimal(cost: float | None) -> Decimal:
    """Convert a float cost (possibly NaN) to a non-negative Decimal."""
    if cost is None:
        return Decimal("0")
    if cost != cost:  # NaN check
        return Decimal("0")
    if cost < 0:
        return Decimal("0")
    # Use string repr to avoid binary float artefacts; quantize to 6dp later in DB.
    return Decimal(repr(cost))
```

- [ ] **Step 4: Run — expect pass**

```
uv run pytest tests/adapters/test_tracking_llm.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/qfa/adapters/tracking_llm.py tests/adapters/test_tracking_llm.py
git commit -m "feat(adapters): add TrackingLLMAdapter recording per-attempt LLM calls"
```

---

## Task 7: Add an Alembic migration extending the `llm_calls` table

**Files:**
- Create: `alembic/versions/<rev>_extend_llm_calls_with_operation_cost_status.py`

- [ ] **Step 1: Generate a new revision**

```
uv run alembic revision -m "extend llm_calls with operation, cost_usd, status, error_class"
```

This creates `alembic/versions/<rev>_extend_llm_calls_with_operation_cost_status.py`.

- [ ] **Step 2: Replace the generated file's contents**

Open the new file (the path will include a generated revision hash) and replace its body with:

```python
"""Extend llm_calls with operation, cost_usd, status, error_class.

Revision ID: <keep the generated id>
Revises: fbeeb04c3d36
Create Date: <keep the generated date>
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers — keep the generated `revision` string already in the file.
revision: str = "<keep>"
down_revision: Union[str, Sequence[str], None] = "fbeeb04c3d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Extend llm_calls with the new columns, constraints, and indexes."""
    op.add_column(
        "llm_calls",
        sa.Column(
            "operation",
            sa.String(length=64),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="ok",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "error_class",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # Drop server defaults — application controls these values now.
    op.alter_column("llm_calls", "operation", server_default=None)
    op.alter_column("llm_calls", "cost_usd", server_default=None)
    op.alter_column("llm_calls", "status", server_default=None)

    # Constraints
    op.create_check_constraint(
        "llm_calls_status_known",
        "llm_calls",
        "status IN ('ok', 'error')",
    )
    op.create_check_constraint(
        "llm_calls_input_tokens_nonneg",
        "llm_calls",
        "input_tokens >= 0",
    )
    op.create_check_constraint(
        "llm_calls_output_tokens_nonneg",
        "llm_calls",
        "output_tokens >= 0",
    )
    op.create_check_constraint(
        "llm_calls_cost_nonneg",
        "llm_calls",
        "cost_usd >= 0",
    )
    op.create_check_constraint(
        "llm_calls_duration_nonneg",
        "llm_calls",
        "call_duration_ms >= 0",
    )
    op.create_check_constraint(
        "llm_calls_error_class_iff_error",
        "llm_calls",
        "(status = 'error') = (error_class IS NOT NULL)",
    )

    # Indexes — drop the old single-column tenant index and replace with the composite.
    op.drop_index("ix_llm_calls_tenant_id", table_name="llm_calls")
    op.create_index(
        "idx_llm_calls_tenant_timestamp",
        "llm_calls",
        ["tenant_id", "timestamp"],
    )
    op.create_index(
        "idx_llm_calls_timestamp",
        "llm_calls",
        ["timestamp"],
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_index("idx_llm_calls_timestamp", table_name="llm_calls")
    op.drop_index("idx_llm_calls_tenant_timestamp", table_name="llm_calls")
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])

    op.drop_constraint("llm_calls_error_class_iff_error", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_duration_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_cost_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_output_tokens_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_input_tokens_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_status_known", "llm_calls", type_="check")

    op.drop_column("llm_calls", "created_at")
    op.drop_column("llm_calls", "error_class")
    op.drop_column("llm_calls", "status")
    op.drop_column("llm_calls", "cost_usd")
    op.drop_column("llm_calls", "operation")
```

(The `<keep>` placeholders refer to the values Alembic generated — leave them as Alembic created them; only edit the body.)

- [ ] **Step 3: Commit (no test run yet — migration is exercised via integration tests in Task 16)**

```
git add alembic/versions/
git commit -m "feat(db): alembic migration extending llm_calls with operation/cost/status"
```

---

## Task 8: Update SQLAlchemy table model and `record_call` to match the new schema

**Files:**
- Modify: `src/qfa/adapters/db.py`
- Modify: `tests/adapters/test_db.py`

- [ ] **Step 1: Update the test for the new record fields**

Replace `_make_record` and the existing tests in `tests/adapters/test_db.py` with:

```python
"""Tests for the SQLAlchemy usage repository (sqlite-compatible only).

Aggregation tests against real PostgreSQL live in ``tests/adapters/
test_db_postgres.py`` and are gated by ``@pytest.mark.integration``.
SQLite cannot evaluate ``percentile_cont`` so we only validate inserts and
the round-trip of every column on the basic insert path.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from qfa.adapters.db import (
    SqlAlchemyUsageRepository,
    create_session_factory,
    llm_calls,
    metadata,
)
from qfa.domain.models import CallStatus, LLMCallRecord, Operation


pytestmark = pytest.mark.asyncio


def _make_record(
    tenant_id: str = "tenant-1",
    operation: Operation = Operation.ANALYZE,
    input_tokens: int = 100,
    output_tokens: int = 50,
    call_duration_ms: int = 500,
    model: str = "gpt-4-test",
    cost_usd: Decimal = Decimal("0.0001"),
    status: CallStatus = CallStatus.OK,
    error_class: str | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        operation=operation,
        timestamp=datetime.now(UTC),
        call_duration_ms=call_duration_ms,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        status=status,
        error_class=error_class,
    )


@pytest.fixture
async def sqlite_repo(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    session_factory = create_session_factory(engine)
    repo = SqlAlchemyUsageRepository(session_factory)
    yield repo, engine
    await engine.dispose()


@pytest.fixture
def needs_aiosqlite():
    pytest.importorskip("aiosqlite")


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_inserts_row(sqlite_repo):
    repo, engine = sqlite_repo
    await repo.record_call(_make_record())
    async with engine.connect() as conn:
        count = (await conn.execute(sa.select(sa.func.count()).select_from(llm_calls))).scalar()
    assert count == 1


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_round_trips_all_fields(sqlite_repo):
    repo, engine = sqlite_repo
    rec = _make_record(
        tenant_id="my-tenant",
        operation=Operation.ASSIGN_CODES,
        input_tokens=42,
        output_tokens=7,
        cost_usd=Decimal("1.234567"),
    )
    await repo.record_call(rec)

    async with engine.connect() as conn:
        row = (await conn.execute(sa.select(llm_calls))).one()

    assert row.tenant_id == "my-tenant"
    assert row.operation == "assign_codes"
    assert row.input_tokens == 42
    assert row.output_tokens == 7
    assert row.status == "ok"
    assert row.error_class is None
    # SQLite stores Numeric as string by default; the repository writes Decimal.
    assert Decimal(str(row.cost_usd)) == Decimal("1.234567")


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_failure_path_persists_error_class(sqlite_repo):
    repo, engine = sqlite_repo
    rec = _make_record(
        status=CallStatus.ERROR,
        error_class="LLMTimeoutError",
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal("0"),
    )
    await repo.record_call(rec)

    async with engine.connect() as conn:
        row = (await conn.execute(sa.select(llm_calls))).one()

    assert row.status == "error"
    assert row.error_class == "LLMTimeoutError"
```

- [ ] **Step 2: Update the SQLAlchemy table definition and `record_call`**

In `src/qfa/adapters/db.py`, replace the `llm_calls` Table definition with:

```python
llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("tenant_id", sa.String(255), nullable=False),
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("call_duration_ms", sa.Integer, nullable=False),
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("output_tokens", sa.Integer, nullable=False, default=0),
    sa.Column(
        "cost_usd",
        sa.Numeric(precision=12, scale=6),
        nullable=False,
        default=0,
    ),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error_class", sa.String(128), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Index("idx_llm_calls_tenant_timestamp", "tenant_id", "timestamp"),
    sa.Index("idx_llm_calls_timestamp", "timestamp"),
)
```

Also update the imports at the top of the file:

```python
from datetime import datetime
from decimal import Decimal
```

Replace the `record_call` method body to insert the new columns:

```python
    async def record_call(self, record: LLMCallRecord) -> None:
        """Insert a single LLM call attempt record."""
        async with self._session_factory() as session:
            await session.execute(
                llm_calls.insert().values(
                    tenant_id=record.tenant_id,
                    operation=str(record.operation),
                    timestamp=record.timestamp,
                    call_duration_ms=record.call_duration_ms,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_usd=record.cost_usd,
                    status=str(record.status),
                    error_class=record.error_class,
                )
            )
            await session.commit()
```

- [ ] **Step 3: Run sqlite-compat tests**

```
uv run pytest tests/adapters/test_db.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```
git add src/qfa/adapters/db.py tests/adapters/test_db.py
git commit -m "feat(db): extend llm_calls table model and record_call for new schema"
```

---

## Task 9: Implement `get_usage_stats` with time filter and `by_operation`

**Files:**
- Modify: `src/qfa/adapters/db.py`

- [ ] **Step 1: Replace `get_usage_stats`**

Replace the existing method on `SqlAlchemyUsageRepository`:

```python
    async def get_usage_stats(
        self,
        tenant_id: str,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> UsageStats | None:
        """Aggregate stats for a single tenant within an optional time window.

        ``cost_usd``, distributions, and token totals scope to ``status='ok'``.
        ``total_calls`` and ``failed_calls`` count all rows in the window.
        """
        base_pred = [llm_calls.c.tenant_id == tenant_id]
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        ok_pred = [*base_pred, llm_calls.c.status == "ok"]

        async with self._session_factory() as session:
            totals = await session.execute(
                sa.select(
                    sa.func.count().label("total_calls"),
                    sa.func.sum(
                        sa.case((llm_calls.c.status == "error", 1), else_=0)
                    ).label("failed_calls"),
                ).where(*base_pred)
            )
            t_row = totals.one()
            total_calls = int(t_row._mapping["total_calls"])
            if total_calls == 0:
                return None
            failed_calls = int(t_row._mapping["failed_calls"] or 0)

            ok_stats = await session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0).label(
                        "total_cost_usd"
                    ),
                    *_build_stats_columns(llm_calls.c.call_duration_ms, "dur"),
                    *_build_stats_columns(llm_calls.c.input_tokens, "inp"),
                    *_build_stats_columns(llm_calls.c.output_tokens, "out"),
                ).where(*ok_pred)
            )
            ok_row = ok_stats.one()

            per_op = (
                await session.execute(
                    sa.select(
                        llm_calls.c.operation,
                        sa.func.count().label("total_calls"),
                        sa.func.sum(
                            sa.case((llm_calls.c.status == "error", 1), else_=0)
                        ).label("failed_calls"),
                        sa.func.coalesce(
                            sa.func.sum(
                                sa.case(
                                    (
                                        llm_calls.c.status == "ok",
                                        llm_calls.c.cost_usd,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("cost_usd"),
                        sa.func.coalesce(
                            sa.func.sum(
                                sa.case(
                                    (
                                        llm_calls.c.status == "ok",
                                        llm_calls.c.input_tokens,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("input_tokens_total"),
                        sa.func.coalesce(
                            sa.func.sum(
                                sa.case(
                                    (
                                        llm_calls.c.status == "ok",
                                        llm_calls.c.output_tokens,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("output_tokens_total"),
                    )
                    .where(*base_pred)
                    .group_by(llm_calls.c.operation)
                )
            ).all()

        return UsageStats(
            tenant_id=tenant_id,
            total_calls=total_calls,
            failed_calls=failed_calls,
            total_cost_usd=Decimal(str(ok_row._mapping["total_cost_usd"])),
            call_duration=_parse_distribution_ok(ok_row, "dur"),
            input_tokens=_parse_token_stats_ok(ok_row, "inp"),
            output_tokens=_parse_token_stats_ok(ok_row, "out"),
            by_operation=_build_by_operation(per_op),
        )
```

- [ ] **Step 2: Add the helper functions used above**

In the same file, replace `_parse_distribution` / `_parse_token_stats` with the OK-aware variants and add `_build_by_operation`:

```python
def _parse_distribution_ok(row: sa.Row, prefix: str) -> DistributionStats:
    """Parse DistributionStats from a row whose aggregates are over ok-only rows.

    When no ok rows exist, all aggregates are NULL → return zeros.
    """
    m = row._mapping
    avg = m[f"{prefix}_avg"]
    if avg is None:
        return DistributionStats(avg=0, min=0, max=0, p5=0, p95=0)
    return DistributionStats(
        avg=float(avg),
        min=float(m[f"{prefix}_min"]),
        max=float(m[f"{prefix}_max"]),
        p5=float(m[f"{prefix}_p5"]),
        p95=float(m[f"{prefix}_p95"]),
    )


def _parse_token_stats_ok(row: sa.Row, prefix: str) -> TokenStats:
    """Parse TokenStats from a row whose aggregates are over ok-only rows."""
    m = row._mapping
    avg = m[f"{prefix}_avg"]
    if avg is None:
        return TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0)
    return TokenStats(
        avg=float(avg),
        min=float(m[f"{prefix}_min"]),
        max=float(m[f"{prefix}_max"]),
        total=int(m[f"{prefix}_sum"] or 0),
        p5=float(m[f"{prefix}_p5"]),
        p95=float(m[f"{prefix}_p95"]),
    )


def _build_by_operation(rows: list[sa.Row]) -> tuple[OperationStats, ...]:
    """Build sorted by-operation entries.

    Sort: cost_usd desc, ties broken by operation asc.
    Unknown operation strings (rows that predate per-op tracking) are
    coerced to ``Operation.UNKNOWN``.
    """
    items: list[OperationStats] = []
    for r in rows:
        m = r._mapping
        op_raw = str(m["operation"])
        try:
            op_enum = Operation(op_raw)
        except ValueError:
            op_enum = Operation.UNKNOWN
        items.append(
            OperationStats(
                operation=op_enum,
                total_calls=int(m["total_calls"]),
                failed_calls=int(m["failed_calls"] or 0),
                cost_usd=Decimal(str(m["cost_usd"])),
                input_tokens_total=int(m["input_tokens_total"] or 0),
                output_tokens_total=int(m["output_tokens_total"] or 0),
            )
        )
    items.sort(key=lambda s: (-s.cost_usd, str(s.operation)))
    return tuple(items)
```

Update the import block in the same file to include the new domain types:

```python
from qfa.domain.models import (
    DistributionStats,
    LLMCallRecord,
    Operation,
    OperationStats,
    TokenStats,
    UsageStats,
)
```

- [ ] **Step 3: Run lint + the existing sqlite-compat tests**

```
uv run pytest tests/adapters/test_db.py -v && uv run ty check
```

Expected: PASS for tests; ty may still complain in unrelated files — fix only the new errors.

- [ ] **Step 4: Commit**

```
git add src/qfa/adapters/db.py
git commit -m "feat(db): add time-filter and by_operation breakdown to get_usage_stats"
```

---

## Task 10: Implement `get_all_usage_stats` with time filter and per-tenant `by_operation`

**Files:**
- Modify: `src/qfa/adapters/db.py`

- [ ] **Step 1: Replace the method**

Replace `get_all_usage_stats` on `SqlAlchemyUsageRepository`:

```python
    async def get_all_usage_stats(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[UsageStats]:
        """Per-tenant stats plus a grand total entry (tenant_id=None).

        Tenants are returned alphabetically by tenant_id; the grand-total
        entry is appended last.
        """
        base_pred: list = []
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        async with self._session_factory() as session:
            tenants = (
                await session.execute(
                    sa.select(llm_calls.c.tenant_id)
                    .where(*base_pred)
                    .group_by(llm_calls.c.tenant_id)
                    .order_by(llm_calls.c.tenant_id.asc())
                )
            ).all()

        results: list[UsageStats] = []
        for trow in tenants:
            tid = trow._mapping["tenant_id"]
            stats = await self.get_usage_stats(tid, from_=from_, to=to)
            if stats is not None:
                results.append(stats)

        # Grand total — reuse get_usage_stats over a NULL tenant_id by inlining the logic.
        async with self._session_factory() as session:
            totals = await session.execute(
                sa.select(
                    sa.func.count().label("total_calls"),
                    sa.func.sum(
                        sa.case((llm_calls.c.status == "error", 1), else_=0)
                    ).label("failed_calls"),
                ).where(*base_pred)
            )
            t_row = totals.one()
            total_calls = int(t_row._mapping["total_calls"])
            if total_calls == 0:
                return results
            failed_calls = int(t_row._mapping["failed_calls"] or 0)

            ok_pred = [*base_pred, llm_calls.c.status == "ok"]
            ok_stats = await session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0).label(
                        "total_cost_usd"
                    ),
                    *_build_stats_columns(llm_calls.c.call_duration_ms, "dur"),
                    *_build_stats_columns(llm_calls.c.input_tokens, "inp"),
                    *_build_stats_columns(llm_calls.c.output_tokens, "out"),
                ).where(*ok_pred)
            )
            ok_row = ok_stats.one()

            per_op = (
                await session.execute(
                    sa.select(
                        llm_calls.c.operation,
                        sa.func.count().label("total_calls"),
                        sa.func.sum(
                            sa.case((llm_calls.c.status == "error", 1), else_=0)
                        ).label("failed_calls"),
                        sa.func.coalesce(
                            sa.func.sum(
                                sa.case(
                                    (
                                        llm_calls.c.status == "ok",
                                        llm_calls.c.cost_usd,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("cost_usd"),
                        sa.func.coalesce(
                            sa.func.sum(
                                sa.case(
                                    (
                                        llm_calls.c.status == "ok",
                                        llm_calls.c.input_tokens,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("input_tokens_total"),
                        sa.func.coalesce(
                            sa.func.sum(
                                sa.case(
                                    (
                                        llm_calls.c.status == "ok",
                                        llm_calls.c.output_tokens,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("output_tokens_total"),
                    )
                    .where(*base_pred)
                    .group_by(llm_calls.c.operation)
                )
            ).all()

        results.append(
            UsageStats(
                tenant_id=None,
                total_calls=total_calls,
                failed_calls=failed_calls,
                total_cost_usd=Decimal(str(ok_row._mapping["total_cost_usd"])),
                call_duration=_parse_distribution_ok(ok_row, "dur"),
                input_tokens=_parse_token_stats_ok(ok_row, "inp"),
                output_tokens=_parse_token_stats_ok(ok_row, "out"),
                by_operation=_build_by_operation(per_op),
            )
        )
        return results
```

- [ ] **Step 2: Run tests + lint**

```
uv run pytest tests/adapters/test_db.py -v && uv run ty check
```

Expected: PASS.

- [ ] **Step 3: Commit**

```
git add src/qfa/adapters/db.py
git commit -m "feat(db): time-filter and per-tenant by_operation in get_all_usage_stats"
```

---

## Task 11: Add `_call_scope` helper and wire each public orchestrator method

**Files:**
- Modify: `src/qfa/services/orchestrator.py`
- Modify: `tests/services/test_orchestrator.py` (add a focused test for scope propagation; existing tests stay)

- [ ] **Step 1: Write a failing test asserting the orchestrator enters call_scope per public method**

Append to `tests/services/test_orchestrator.py` (placement: end of file, with appropriate imports at top):

```python
from qfa.adapters.call_context import current_call_context
from qfa.domain.models import Operation


class _ContextRecordingLLM:
    """LLMPort fake that records the current_call_context at every call."""

    def __init__(self) -> None:
        self.contexts: list = []

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ):
        self.contexts.append(current_call_context.get())
        from qfa.domain.models import LLMResponse

        return LLMResponse(
            text='{"title":"t","summary":"- s"}',
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


@pytest.mark.asyncio
async def test_analyze_enters_call_scope_with_analyze_operation():
    llm = _ContextRecordingLLM()
    orch = StandardOrchestrator(
        llm=llm,
        settings=OrchestratorSettings(),
        llm_timeout_seconds=10.0,
        max_total_tokens=10_000,
    )
    from datetime import UTC, datetime, timedelta

    from qfa.domain import FeedbackItem
    from qfa.domain.models import AnalysisRequest

    req = AnalysisRequest(
        documents=(FeedbackItem(id="d1", text="hello"),),
        prompt="p",
        tenant_id="t1",
    )

    await orch.analyze(req, datetime.now(UTC) + timedelta(seconds=60), anonymize=False)

    assert len(llm.contexts) >= 1
    assert all(ctx is not None for ctx in llm.contexts)
    assert llm.contexts[0].operation == Operation.ANALYZE
    assert llm.contexts[0].tenant_id == "t1"
```

(Add the necessary imports at the top of the file: `import pytest` and `from qfa.services.orchestrator import StandardOrchestrator` if missing; `from qfa.settings import OrchestratorSettings`.)

- [ ] **Step 2: Run — expect failure (no scope is entered yet)**

```
uv run pytest tests/services/test_orchestrator.py -k "test_analyze_enters_call_scope" -v
```

Expected: assertion FAIL — context is `None`.

- [ ] **Step 3: Add the helper and wrap each public method**

In `src/qfa/services/orchestrator.py`:

Add at the top with other imports:

```python
from qfa.adapters.call_context import call_scope
from qfa.domain.models import Operation
```

Wrap each public method's body in an `async with call_scope(...)` block. Replace the existing four method bodies:

```python
    async def analyze(
        self,
        request: AnalysisRequest,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResult:
        async with call_scope(tenant_id=request.tenant_id, operation=Operation.ANALYZE):
            self._check_injection(request.documents)
            system_message = _SYSTEM_MESSAGE_TEMPLATE.format(prompt=request.prompt)
            user_message = self._assemble_documents(request.documents)
            self._check_token_limit(system_message, user_message)
            return await self._call_with_retries(
                system_message=system_message,
                user_message=user_message,
                tenant_id=request.tenant_id,
                deadline=deadline,
                anonymize=anonymize,
            )

    async def summarize(
        self,
        request: SummaryRequest,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SummaryResult:
        async with call_scope(
            tenant_id=request.tenant_id, operation=Operation.SUMMARIZE
        ):
            return await self._summarize_inner(request, deadline, anonymize)

    async def summarize_aggregate(
        self,
        request: SummaryRequest,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AggregateSummaryResult:
        async with call_scope(
            tenant_id=request.tenant_id, operation=Operation.SUMMARIZE_AGGREGATE
        ):
            return await self._summarize_aggregate_inner(request, deadline, anonymize)

    async def assign_codes(
        self,
        request: CodingAssignmentRequest,
        deadline: datetime,
    ) -> CodingAssignmentResult:
        async with call_scope(
            tenant_id=request.tenant_id, operation=Operation.ASSIGN_CODES
        ):
            return await self._assign_codes_inner(request, deadline)
```

Then move the existing bodies (everything between docstring and `return ...`) of `summarize`, `summarize_aggregate`, and `assign_codes` into private `_summarize_inner`, `_summarize_aggregate_inner`, `_assign_codes_inner` methods that take the same arguments. The simplest mechanical refactor is:

1. Rename the original `summarize` body to `_summarize_inner(self, request, deadline, anonymize)` keeping the existing implementation verbatim.
2. Same for `_summarize_aggregate_inner(self, request, deadline, anonymize=True)` — note: original `summarize_aggregate` did not take an `anonymize` kwarg. Add it to both the new public method (default True) and pass through to inner.
3. Same for `_assign_codes_inner(self, request, deadline)`.

For `summarize_aggregate`, also pass `anonymize` into the two `_call_with_retries` invocations inside the inner — they currently do not pass it (default True is fine; if you keep the existing behavior, leave as-is — the new public method just delegates).

- [ ] **Step 4: Run — expect pass**

```
uv run pytest tests/services/test_orchestrator.py -v
```

Expected: PASS for the new test and all existing orchestrator tests.

- [ ] **Step 5: Commit**

```
git add src/qfa/services/orchestrator.py tests/services/test_orchestrator.py
git commit -m "feat(orchestrator): enter call_scope per public method to set tracking context"
```

---

## Task 12: Delete `TrackingOrchestrator` and its tests

**Files:**
- Delete: `src/qfa/services/tracking_orchestrator.py`
- Delete: `tests/services/test_tracking_orchestrator.py`

- [ ] **Step 1: Remove the files**

```
git rm src/qfa/services/tracking_orchestrator.py tests/services/test_tracking_orchestrator.py
```

- [ ] **Step 2: Run pytest — expect pass**

```
uv run pytest -q
```

Expected: PASS. Existing imports of `TrackingOrchestrator` (only inside `qfa.api.app.lifespan`) will be replaced in Task 14; the module can be missing now because that import is inside the `if settings.db.track_usage` branch which the unit-test path does not exercise.

- [ ] **Step 3: Commit**

```
git commit -m "refactor: drop TrackingOrchestrator superseded by TrackingLLMAdapter"
```

---

## Task 13: Add settings cross-validation for `DB_URL` when `DB_TRACK_USAGE=true`

**Files:**
- Modify: `src/qfa/settings.py`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_settings.py`:

```python
def test_database_settings_requires_url_when_track_usage_true(monkeypatch):
    monkeypatch.setenv("DB_TRACK_USAGE", "true")
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("AUTH_API_KEYS", '[]')
    with pytest.raises(Exception):
        AppSettings()


def test_database_settings_accepts_url_when_track_usage_true(monkeypatch):
    monkeypatch.setenv("DB_TRACK_USAGE", "true")
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("AUTH_API_KEYS", '[]')
    s = AppSettings()
    assert s.db.url == "postgresql+asyncpg://u:p@localhost/db"
    assert s.db.track_usage is True
```

(Make sure `pytest`, `AppSettings` are imported.)

- [ ] **Step 2: Run — expect failure (no validator yet)**

```
uv run pytest tests/test_settings.py -k "track_usage" -v
```

Expected: FAIL.

- [ ] **Step 3: Add the validator**

In `src/qfa/settings.py`, add `model_validator` to imports:

```python
from pydantic import Field, SecretStr, field_validator, model_validator
```

Append a validator to `DatabaseSettings`:

```python
    @model_validator(mode="after")
    def _require_url_when_track_usage(self) -> "DatabaseSettings":
        if self.track_usage and not self.url:
            raise ValueError(
                "DB_URL must be set when DB_TRACK_USAGE=true"
            )
        return self
```

- [ ] **Step 4: Run — expect pass**

```
uv run pytest tests/test_settings.py -k "track_usage" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/qfa/settings.py tests/test_settings.py
git commit -m "feat(settings): require DB_URL when DB_TRACK_USAGE is enabled"
```

---

## Task 14: Wire `TrackingLLMAdapter` and advisory-lock-guarded migrations at lifespan startup

**Files:**
- Modify: `src/qfa/api/app.py`
- Create: `src/qfa/adapters/migrations.py`

- [ ] **Step 1: Create the migrations helper**

Create `src/qfa/adapters/migrations.py`:

```python
"""App-startup migration runner guarded by a Postgres advisory lock.

Used when ``DB_TRACK_USAGE=true``. Multiple replicas may all attempt to
``alembic upgrade head`` at startup; the advisory lock serialises them.
The lock is session-scoped, so a crashed migrator releases on connection
close.
"""

from __future__ import annotations

import logging
from pathlib import Path

import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

#: Stable 64-bit integer key for the migration advisory lock.
LLM_CALLS_MIGRATION_LOCK_KEY: int = 7424901234567890


async def upgrade_to_head(engine: AsyncEngine, db_url: str) -> None:
    """Run ``alembic upgrade head`` under a Postgres advisory lock.

    Parameters
    ----------
    engine : AsyncEngine
        Async engine used to acquire the lock and probe connectivity.
    db_url : str
        URL passed to Alembic's offline config (must include async driver).
    """
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("SELECT pg_advisory_lock(:k)"),
            {"k": LLM_CALLS_MIGRATION_LOCK_KEY},
        )
        try:
            cfg = _build_alembic_config(db_url)
            await conn.run_sync(lambda sync_conn: _run_upgrade(cfg, sync_conn))
        finally:
            await conn.execute(
                sa.text("SELECT pg_advisory_unlock(:k)"),
                {"k": LLM_CALLS_MIGRATION_LOCK_KEY},
            )

    # Probe connectivity post-migration; fail fast if the schema isn't reachable.
    async with engine.connect() as probe:
        await probe.execute(sa.text("SELECT 1"))


def _build_alembic_config(db_url: str) -> AlembicConfig:
    repo_root = Path(__file__).resolve().parents[3]
    cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _run_upgrade(cfg: AlembicConfig, sync_conn) -> None:
    cfg.attributes["connection"] = sync_conn
    alembic_command.upgrade(cfg, "head")
```

Adjust `alembic/env.py` to use `cfg.attributes["connection"]` when present so the migration runs on the already-locked connection. Replace the body of `run_migrations_online` and supporting helpers with:

```python
def run_migrations_online() -> None:
    """Run migrations in 'online' mode; reuse a passed-in connection if any."""
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        do_run_migrations(connectable)
    else:
        asyncio.run(run_async_migrations())
```

Keep the rest of the file unchanged.

- [ ] **Step 2: Update `qfa.api.app.lifespan`**

In `src/qfa/api/app.py`:

Add to imports:

```python
from qfa.adapters.tracking_llm import TrackingLLMAdapter
```

Replace the `lifespan` body's DB-on branch (the `if settings.db.track_usage:` block and the assignments below it):

```python
    settings = AppSettings()
    setup_logging(settings.log)

    _register_custom_model_prices()

    api_keys = settings.auth.api_keys

    engine = None
    usage_repo = None

    base_llm = build_llm_client(settings.llm)
    llm_for_orch: object = base_llm

    if settings.db.track_usage:
        from qfa.adapters.db import (
            SqlAlchemyUsageRepository,
            create_async_engine_from_url,
            create_session_factory,
        )
        from qfa.adapters.migrations import upgrade_to_head

        engine = create_async_engine_from_url(settings.db.url)
        await upgrade_to_head(engine, settings.db.url)

        session_factory = create_session_factory(engine)
        usage_repo = SqlAlchemyUsageRepository(session_factory)
        llm_for_orch = TrackingLLMAdapter(inner=base_llm, usage_repo=usage_repo)
        logger.info("Usage tracking enabled (per-attempt, per-operation)")

    orchestrator: OrchestratorPort = StandardOrchestrator(
        llm=llm_for_orch,  # type: ignore[arg-type]
        settings=settings.orchestrator,
        llm_timeout_seconds=settings.llm.timeout_seconds,
        max_total_tokens=settings.llm.max_total_tokens,
    )

    app.state.orchestrator = orchestrator
    app.state.api_keys = api_keys
    app.state.settings = settings
    app.state.usage_repo = usage_repo

    yield

    if engine is not None:
        await engine.dispose()
```

Update the `create_async_engine_from_url` helper in `src/qfa/adapters/db.py` to apply pool tuning:

```python
def create_async_engine_from_url(url: str) -> AsyncEngine:
    """Create a tuned async engine with pre-ping + recycle for Azure managed PG."""
    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
```

- [ ] **Step 3: Run unit tests**

```
uv run pytest -q
```

Expected: PASS. Lifespan code is not exercised by unit tests — this is a wiring change.

- [ ] **Step 4: Commit**

```
git add src/qfa/api/app.py src/qfa/adapters/migrations.py src/qfa/adapters/db.py alembic/env.py
git commit -m "feat(app): wire TrackingLLMAdapter with advisory-lock-guarded migrations at startup"
```

---

## Task 15: Extend API schemas with cost/failure/by_operation/time-filter fields

**Files:**
- Modify: `src/qfa/api/schemas.py`

- [ ] **Step 1: Add new schemas**

In `src/qfa/api/schemas.py`, add at the top:

```python
from datetime import datetime
from decimal import Decimal
```

Add a new `OperationStatsResponse`:

```python
class OperationStatsResponse(BaseModel):
    """Per-operation aggregated stats."""

    operation: str = Field(description="Orchestrator operation.")
    total_calls: int
    failed_calls: int
    cost_usd: Decimal = Field(description="Sum of cost for status='ok' rows.")
    input_tokens_total: int
    output_tokens_total: int

    @field_serializer("cost_usd")
    def _serialize_cost(self, v: Decimal) -> float:
        return float(v)
```

Update `UsageStatsResponse`:

```python
class UsageStatsResponse(BaseModel):
    """Aggregated usage statistics for a single tenant or grand total."""

    tenant_id: str | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    total_calls: int
    failed_calls: int
    total_cost_usd: Decimal
    call_duration: DistributionStatsResponse
    input_tokens: TokenStatsResponse
    output_tokens: TokenStatsResponse
    by_operation: list[OperationStatsResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_serializer("total_cost_usd")
    def _serialize_total_cost(self, v: Decimal) -> float:
        return float(v)
```

Update `AllUsageStatsResponse`:

```python
class AllUsageStatsResponse(BaseModel):
    """Per-tenant + grand total usage with optional echoed time window."""

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    tenants: list[UsageStatsResponse]
    total: UsageStatsResponse

    model_config = {"populate_by_name": True}
```

Update the imports at the top of the schemas file to include `field_serializer`:

```python
from pydantic import BaseModel, Field, field_serializer, model_validator
```

- [ ] **Step 2: Run lint**

```
uv run ty check
```

Expected: any new schema-related errors fixed; orchestrator-routes wiring (Task 16) catches the rest.

- [ ] **Step 3: Commit**

```
git add src/qfa/api/schemas.py
git commit -m "feat(api): extend usage response schemas with cost, failure, by_operation, time window"
```

---

## Task 16: Update `/v1/usage` and `/v1/usage/all` to accept time-filter and return new shape

**Files:**
- Modify: `src/qfa/api/routes.py`
- Modify: `src/qfa/api/dependencies.py`
- Modify: `tests/api/test_usage_routes.py`

- [ ] **Step 1: Update tests for the new shape and time filter**

Replace the entire contents of `tests/api/test_usage_routes.py` with:

```python
"""Tests for usage tracking API endpoints."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from qfa.domain.models import (
    DistributionStats,
    Operation,
    OperationStats,
    TenantApiKey,
    TokenStats,
    UsageStats,
)


FAKE_API_KEY = "test-key-abc123"
FAKE_SUPERUSER_KEY = "superuser-key-xyz789"

pytestmark = pytest.mark.asyncio


def _make_usage_stats(tenant_id: str | None = "tenant-test", total_calls: int = 5):
    return UsageStats(
        tenant_id=tenant_id,
        total_calls=total_calls,
        failed_calls=1,
        total_cost_usd=Decimal("0.500000"),
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
        by_operation=(
            OperationStats(
                operation=Operation.ANALYZE,
                total_calls=4,
                failed_calls=1,
                cost_usd=Decimal("0.4"),
                input_tokens_total=2000,
                output_tokens_total=800,
            ),
            OperationStats(
                operation=Operation.SUMMARIZE,
                total_calls=1,
                failed_calls=0,
                cost_usd=Decimal("0.1"),
                input_tokens_total=500,
                output_tokens_total=200,
            ),
        ),
    )


class FakeUsageRepository:
    def __init__(self, stats=None, all_stats=None):
        self._stats = stats
        self._all_stats = all_stats or []
        self.last_args: tuple = ()

    async def record_call(self, record):
        pass

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        self.last_args = (tenant_id, from_, to)
        return self._stats

    async def get_all_usage_stats(self, from_=None, to=None):
        self.last_args = (from_, to)
        return self._all_stats


class TestUsageDisabled:
    async def test_usage_503_when_disabled(self, client):
        resp = await client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_tracking_disabled"

    async def test_usage_all_503_when_disabled(self, test_app, client):
        test_app.state.api_keys.append(
            TenantApiKey(
                name="superuser",
                key=FAKE_SUPERUSER_KEY,
                tenant_id="admin",
                is_superuser=True,
                key_id="admin-0",
            )
        )
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_tracking_disabled"


class TestUsageEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        repo = FakeUsageRepository(stats=_make_usage_stats())
        test_app.state.usage_repo = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c, repo

    async def test_returns_200_with_new_shape(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 5
        assert data["failed_calls"] == 1
        assert data["total_cost_usd"] == 0.5
        assert data["tenant_id"] == "tenant-test"
        assert len(data["by_operation"]) == 2
        assert data["by_operation"][0]["operation"] == "analyze"

    async def test_passes_time_filter_to_repo(self, client_with_repo):
        client, repo = client_with_repo
        resp = await client.get(
            "/v1/usage",
            params={"from": "2026-04-01T00:00:00Z", "to": "2026-05-01T00:00:00Z"},
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 200
        _, from_, to = repo.last_args
        assert from_ == datetime(2026, 4, 1, tzinfo=UTC)
        assert to == datetime(2026, 5, 1, tzinfo=UTC)

    async def test_rejects_naive_datetime(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            params={"from": "2026-04-01T00:00:00"},
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 422

    async def test_rejects_to_not_strictly_greater_than_from(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            params={
                "from": "2026-05-01T00:00:00Z",
                "to": "2026-05-01T00:00:00Z",
            },
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 422

    async def test_empty_window_returns_200_zeros(self, test_app):
        test_app.state.usage_repo = FakeUsageRepository(stats=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/v1/usage",
                headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 0
        assert data["failed_calls"] == 0
        assert data["total_cost_usd"] == 0
        assert data["by_operation"] == []


class TestUsageAllEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        test_app.state.api_keys.append(
            TenantApiKey(
                name="superuser",
                key=FAKE_SUPERUSER_KEY,
                tenant_id="admin",
                is_superuser=True,
                key_id="admin-0",
            )
        )
        all_stats = [
            _make_usage_stats(tenant_id="t1", total_calls=3),
            _make_usage_stats(tenant_id="t2", total_calls=7),
            _make_usage_stats(tenant_id=None, total_calls=10),
        ]
        repo = FakeUsageRepository(all_stats=all_stats)
        test_app.state.usage_repo = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c, repo

    async def test_200_for_superuser_with_total_row(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tenants"]) == 2
        assert data["total"]["total_calls"] == 10
        assert data["total"]["tenant_id"] is None

    async def test_403_for_non_superuser(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Update the `get_usage_repo` dependency to surface a structured 503**

In `src/qfa/api/dependencies.py`, replace the body:

```python
def get_usage_repo(request: Request) -> UsageRepositoryPort:
    """Return the usage repository, or raise 503 with a stable reason code."""
    repo = getattr(request.app.state, "usage_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "usage_tracking_disabled",
                "message": "Usage tracking is not enabled",
            },
        )
    return repo
```

(The route layer wraps this into the `ErrorResponse` envelope below.)

- [ ] **Step 3: Update the routes to accept time-filter, validate it, and return the new shape**

In `src/qfa/api/routes.py`, replace the imports header:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
```

Add a helper near the other private helpers:

```python
def _parse_time_window(
    from_: datetime | None, to: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Validate and normalise the ``from``/``to`` query window.

    Both must be timezone-aware; ``to`` must be strictly greater than ``from``.
    """
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "validation_error",
                    "message": f"{name!r} must be timezone-aware",
                },
            )
    if from_ is not None and to is not None and to <= from_:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "'to' must be strictly greater than 'from'",
            },
        )
    if from_ is not None:
        from_ = from_.astimezone(UTC)
    if to is not None:
        to = to.astimezone(UTC)
    return from_, to


def _zero_usage(tenant_id: str | None) -> UsageStatsResponse:
    return UsageStatsResponse(
        tenant_id=tenant_id,
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStatsResponse(avg=0, min=0, max=0, p5=0, p95=0),
        input_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        by_operation=[],
    )
```

Update `_to_usage_response` to include the new fields:

```python
def _to_usage_response(stats: UsageStats) -> UsageStatsResponse:
    return UsageStatsResponse(
        tenant_id=stats.tenant_id,
        total_calls=stats.total_calls,
        failed_calls=stats.failed_calls,
        total_cost_usd=stats.total_cost_usd,
        call_duration=_to_distribution_response(stats.call_duration),
        input_tokens=_to_token_response(stats.input_tokens),
        output_tokens=_to_token_response(stats.output_tokens),
        by_operation=[
            OperationStatsResponse(
                operation=str(op.operation),
                total_calls=op.total_calls,
                failed_calls=op.failed_calls,
                cost_usd=op.cost_usd,
                input_tokens_total=op.input_tokens_total,
                output_tokens_total=op.output_tokens_total,
            )
            for op in stats.by_operation
        ],
    )
```

Replace the `usage` and `usage_all` route bodies:

```python
@router.get("/v1/usage", response_model=UsageStatsResponse, status_code=200)
async def usage(
    tenant: TenantApiKey = Depends(authenticate_request),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
) -> UsageStatsResponse:
    """Usage statistics for the authenticated tenant within an optional window."""
    from_, to = _parse_time_window(from_, to)
    stats = await usage_repo.get_usage_stats(tenant.tenant_id, from_=from_, to=to)
    if stats is None:
        resp = _zero_usage(tenant.tenant_id)
    else:
        resp = _to_usage_response(stats)
    return resp.model_copy(update={"from_": from_, "to": to})


@router.get("/v1/usage/all", response_model=AllUsageStatsResponse, status_code=200)
async def usage_all(
    _tenant: TenantApiKey = Depends(require_superuser),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
) -> AllUsageStatsResponse:
    """Per-tenant and grand-total usage statistics."""
    from_, to = _parse_time_window(from_, to)
    all_stats = await usage_repo.get_all_usage_stats(from_=from_, to=to)
    tenants = [_to_usage_response(s) for s in all_stats if s.tenant_id is not None]
    total_entry = next((s for s in all_stats if s.tenant_id is None), None)
    total = _to_usage_response(total_entry) if total_entry is not None else _zero_usage(None)
    return AllUsageStatsResponse(
        tenants=tenants,
        total=total,
        from_=from_,
        to=to,
    )
```

Add `OperationStatsResponse` to the imports:

```python
from qfa.api.schemas import (
    ...
    OperationStatsResponse,
    ...
)
```

- [ ] **Step 4: Wire the 503 detail dict through the global exception handler**

The current exception handler for `HTTPException` is FastAPI's default — when `detail` is a dict, FastAPI returns it verbatim, which would not match our `ErrorResponse` envelope. Add a handler so all `HTTPException` instances with a dict `detail` are wrapped consistently:

In `src/qfa/api/app.py`:

```python
from fastapi import HTTPException
```

Add the handler:

```python
async def _handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Wrap HTTPException with our standard error envelope when detail is a dict."""
    detail = exc.detail
    if isinstance(detail, dict):
        body = ErrorResponse(
            error=ErrorDetail(
                code=str(detail.get("code", "http_error")),
                message=str(detail.get("message", "")),
                request_id=_get_request_id(request),
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())
    body = ErrorResponse(
        error=ErrorDetail(
            code="http_error",
            message=str(detail) if detail is not None else "",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())
```

Register it in `register_exception_handlers`:

```python
    app.add_exception_handler(HTTPException, _handle_http_exception)  # type: ignore[arg-type]
```

- [ ] **Step 5: Run all tests + lint**

```
uv run pytest -q && uv run ty check
```

Expected: PASS.

- [ ] **Step 6: Commit**

```
git add src/qfa/api/routes.py src/qfa/api/dependencies.py src/qfa/api/app.py tests/api/test_usage_routes.py
git commit -m "feat(api): add time-filter, by_operation, and structured 503 to /v1/usage[/all]"
```

---

## Task 17: Add Postgres-only integration markers to pyproject and Makefile targets

**Files:**
- Modify: `pyproject.toml`
- Modify: `Makefile`

- [ ] **Step 1: Add markers and addopts**

In `pyproject.toml`, replace the `[tool.pytest.ini_options]` block:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: requires a real Postgres instance (excluded by default)",
    "e2e: end-to-end test with respx-mocked LiteLLM (excluded by default)",
]
addopts = "-m 'not integration and not e2e'"
```

- [ ] **Step 2: Add Make targets**

Append to `Makefile`:

```makefile
.PHONY: db-up db-down db-reset migrate test-integration

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d postgres

migrate:
	uv run alembic upgrade head

test-integration:
	uv run pytest -m "integration or e2e"
```

- [ ] **Step 3: Run unit tests to ensure markers don't accidentally exclude unit tests**

```
uv run pytest -q
```

Expected: PASS — same set of tests as before.

- [ ] **Step 4: Commit**

```
git add pyproject.toml Makefile
git commit -m "build: add integration/e2e pytest markers and DB Make targets"
```

---

## Task 18: Final lint + test sweep, then push

**Files:** none (verification only)

- [ ] **Step 1: Format**

```
uv run ruff format src tests
```

- [ ] **Step 2: Ruff check**

```
uv run ruff check --fix src tests
```

- [ ] **Step 3: ty**

```
uv run ty check
```

If ty surfaces residual errors that originate in code not touched by this plan **and** not in `infra/`, fix them. If they originate in `infra/`, leave them.

- [ ] **Step 4: Pytest**

```
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit any formatter-only changes**

```
git add -A
git diff --cached --stat
git commit -m "style: apply ruff format" || true
```

- [ ] **Step 6: Push and watch CI**

```
git push
gh pr checks 13 --watch
```

If push is rejected because the other developer's commits exist on origin, do:

```
git pull --no-rebase origin feat/token-usage-tracking
# resolve any conflicts that surface in our files (NOT infra/)
git push
```

If CI fails on something originating outside `infra/` and outside the feature scope, investigate and fix. If CI fails on something inside `infra/`, leave it — that is the other developer's domain.

---

## Self-review checklist (run after writing the plan)

- **Spec coverage:**
  - Operation/CallStatus/CallContext/MissingCallScopeError → Task 1 ✓
  - LLMCallRecord shape with cost/status/error_class invariant → Task 2 ✓
  - OperationStats + UsageStats new fields → Task 3 ✓
  - ContextVar + call_scope → Task 4 ✓
  - UsageRepositoryPort time-filter → Task 5 ✓
  - TrackingLLMAdapter (success, failure, scope-missing, recording-failure) → Task 6 ✓
  - Alembic migration extending llm_calls → Task 7 ✓
  - SqlAlchemyUsageRepository record_call → Task 8 ✓
  - get_usage_stats with α policy + by_operation → Task 9 ✓
  - get_all_usage_stats + grand total → Task 10 ✓
  - Orchestrator call_scope wiring → Task 11 ✓
  - TrackingOrchestrator deletion → Task 12 ✓
  - Settings cross-validation → Task 13 ✓
  - Lifespan startup with advisory-lock-guarded migration + TrackingLLMAdapter wiring → Task 14 ✓
  - API schemas → Task 15 ✓
  - API routes time-filter + 503 reason code → Task 16 ✓
  - Test markers + Makefile → Task 17 ✓

- **Out of scope (per spec):** Tier-2 Postgres aggregation tests, Tier-3 e2e respx tests, advisory-lock concurrency tests, table partitioning, retention, outbox, sub-operation labels — explicitly deferred. Default `make test` does not require Docker.

- **Type consistency check:** All references to `OperationStats`/`OperationStatsResponse`, `CallContext`, `Operation`, `CallStatus`, `LLMCallRecord` use their final shapes. The repository's `get_*` signatures match the `UsageRepositoryPort` protocol after Task 5.
