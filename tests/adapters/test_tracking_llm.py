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
            timeout=10.0,
            tenant_id="t1",
        )

    assert repo.attempts == 3
    assert len(repo.records) == 1


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
            timeout=10.0,
            tenant_id="t1",
        )

    assert result.text == "hello"
    assert repo.attempts == 1
    assert repo.records == []
