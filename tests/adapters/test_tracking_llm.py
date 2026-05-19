"""Tests for TrackingLLMAdapter."""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from qfa.adapters.tracking_llm import TrackingLLMAdapter
from qfa.domain.errors import LLMError
from qfa.domain.models import (
    CallStatus,
    LLMCallRecord,
    LLMResponse,
    Operation,
)
from qfa.services.call_context import call_scope, request_id_scope

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _ambient_request_scope() -> AsyncIterator[None]:
    """Wrap every test in a ``request_id_scope`` so ``call_scope`` works.

    These tests don't care about correlating to an HTTP X-Request-ID;
    they just need a valid request scope so the orchestrator's
    ``call_scope`` can resolve a ``call_id`` without raising
    ``MissingRequestScopeError``. Mirrors production where every
    orchestrator entry happens inside a request scope (set by
    ``RequestIdMiddleware`` for HTTP, or by the caller for CLI/jobs).
    """
    async with request_id_scope(uuid4()):
        yield


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
        tenant_id: str,
        response_model=str,
        timeout: float = 20.0,
    ) -> LLMResponse:
        self.calls.append(
            (system_message, user_message, tenant_id, response_model, timeout)
        )
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
        structured="hello",
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
            tenant_id="t1",
            response_model=str,
            timeout=10.0,
        )

    assert result.structured == "hello"
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
                tenant_id="t1",
                response_model=str,
                timeout=10.0,
            )

    assert len(repo.records) == 1
    rec = repo.records[0]
    assert rec.status == CallStatus.ERROR
    assert rec.error_class == "LLMError"
    assert rec.cost_usd == Decimal("0")
    assert rec.input_tokens == 0
    assert rec.output_tokens == 0
    assert rec.operation == Operation.SUMMARIZE


async def test_bypasses_persistence_and_logs_when_call_scope_unset(caplog):
    """Without a call_scope the inner LLM still runs; persistence is skipped + logged.

    Observability must never break the use case: missing scope is a
    wiring bug, but routing the request through to the inner LLM
    preserves user-facing availability. The skipped persistence is
    flagged at ERROR so log-based alerting can fire on the underlying
    wiring bug.
    """
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    with caplog.at_level("ERROR", logger="qfa.adapters.tracking_llm"):
        result = await adapter.complete(
            system_message="sys",
            user_message="usr",
            tenant_id="t1",
            response_model=str,
            timeout=10.0,
        )

    assert result.structured == "hello"
    assert len(inner.calls) == 1
    assert repo.records == []
    assert any("call_scope" in r.message for r in caplog.records)


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
            tenant_id="t1",
            response_model=str,
            timeout=10.0,
        )

    assert result.structured == "hello"


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
                tenant_id="t1",
                response_model=str,
                timeout=10.0,
            )


class _FlakyRepo:
    """Fails with ``exc`` for the first ``fail_times`` attempts, then succeeds."""

    def __init__(self, exc: Exception, fail_times: int) -> None:
        self._exc = exc
        self._remaining = fail_times
        self.attempts = 0
        self.records: list[LLMCallRecord] = []

    async def record_call(self, record: LLMCallRecord) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        self.records.append(record)

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        raise NotImplementedError

    async def get_all_usage_stats(self, from_=None, to=None):
        raise NotImplementedError


def _operational_error() -> Exception:
    from sqlalchemy.exc import OperationalError

    return OperationalError("INSERT", {}, Exception("connection reset"))


async def test_record_retries_transient_operational_error_and_eventually_persists():
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    # Fail twice with a connection-class error, then succeed on attempt 3.
    repo = _FlakyRepo(exc=_operational_error(), fail_times=2)
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        await adapter.complete(
            system_message="sys",
            user_message="usr",
            tenant_id="t1",
            response_model=str,
            timeout=10.0,
        )

    assert repo.attempts == 3
    assert len(repo.records) == 1


async def test_records_copy_call_id_from_context():
    """The adapter must copy ``ctx.call_id`` into the persisted record.

    The whole point of carrying ``call_id`` on ``CallContext`` is for the
    tracking adapter to stamp it onto every row; verify that wiring.
    """
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)
    fixed = uuid4()

    async with request_id_scope(fixed):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
            await adapter.complete(
                system_message="sys",
                user_message="usr",
                tenant_id="t1",
                response_model=str,
                timeout=10.0,
            )

    assert len(repo.records) == 1
    assert repo.records[0].call_id == fixed


async def test_error_path_record_copies_call_id_from_context():
    """The error path must also propagate ``ctx.call_id`` into the record.

    Failed LLM calls still need correlation so #91's aggregation can
    attribute them to the originating API invocation.
    """
    inner = FakeLLMPort()
    inner.queue_failure(LLMError("boom"))
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)
    fixed = uuid4()

    async with request_id_scope(fixed):
        async with call_scope(tenant_id="t1", operation=Operation.SUMMARIZE):
            with pytest.raises(LLMError):
                await adapter.complete(
                    system_message="sys",
                    user_message="usr",
                    tenant_id="t1",
                    response_model=str,
                    timeout=10.0,
                )

    assert len(repo.records) == 1
    assert repo.records[0].call_id == fixed


async def test_multiple_calls_in_one_scope_share_call_id():
    """Two LLM calls under one call_scope must persist the same call_id.

    This is the property #91's aggregation relies on: "sum cost per
    invocation" = group by call_id.
    """

    class _ReusableFake(FakeLLMPort):
        async def complete(self, *args, **kwargs):
            self._next_response = _ok_response()
            return await super().complete(*args, **kwargs)

    inner = _ReusableFake()
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        await adapter.complete(
            system_message="sys",
            user_message="u1",
            tenant_id="t1",
            response_model=str,
        )
        await adapter.complete(
            system_message="sys",
            user_message="u2",
            tenant_id="t1",
            response_model=str,
        )

    assert len(repo.records) == 2
    assert repo.records[0].call_id == repo.records[1].call_id


async def test_parallel_fanout_calls_share_call_id():
    """Parallel LLM calls via asyncio.gather inherit the same call_id.

    ContextVars are snapshot-on-spawn for asyncio tasks, so any fan-out
    pattern (gather, create_task) inside a call_scope sees the same context.
    """

    class _ReusableFake(FakeLLMPort):
        async def complete(self, *args, **kwargs):
            self._next_response = _ok_response()
            return await super().complete(*args, **kwargs)

    inner = _ReusableFake()
    repo = FakeUsageRepository()
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        await asyncio.gather(
            adapter.complete(
                system_message="sys",
                user_message="u1",
                tenant_id="t1",
                response_model=str,
            ),
            adapter.complete(
                system_message="sys",
                user_message="u2",
                tenant_id="t1",
                response_model=str,
            ),
            adapter.complete(
                system_message="sys",
                user_message="u3",
                tenant_id="t1",
                response_model=str,
            ),
        )

    assert len(repo.records) == 3
    ids = {r.call_id for r in repo.records}
    assert len(ids) == 1


async def test_record_does_not_retry_non_transient_runtime_error():
    inner = FakeLLMPort()
    inner.queue_response(_ok_response())
    # RuntimeError is not in the retry-eligible set; should be swallowed once.
    repo = _FlakyRepo(exc=RuntimeError("schema mismatch"), fail_times=10)
    adapter = TrackingLLMAdapter(inner=inner, usage_repo=repo)

    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        result = await adapter.complete(
            system_message="sys",
            user_message="usr",
            tenant_id="t1",
            response_model=str,
            timeout=10.0,
        )

    assert result.structured == "hello"
    assert repo.attempts == 1
    assert repo.records == []
