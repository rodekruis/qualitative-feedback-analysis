"""Tests for :class:`~qfa.services.llm_call_executor.LLMCallExecutor`.

Why: this collaborator is the shared LLM-call scaffolding every use-case
service in ``qfa.services`` delegates to (ADR-017). Before this module its
four behaviours were only reachable through ``Orchestrator``, so a change
to the deadline arithmetic or the token estimate was verified indirectly,
through whichever endpoint happened to exercise it. These tests pin them
directly.

Per ADR-017 the executor under test is the **real** one, constructed over
the existing ``FakeLLMPort`` / ``FakeAnonymizer`` doubles from
``test_orchestrator`` — there is no fake executor.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from qfa.domain.errors import (
    AnalysisTimeoutError,
    FeedbackTooLargeError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import (
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    LLMResponse,
)
from qfa.domain.ports import AnonymizationPort
from qfa.services.llm_call_executor import LLMCallExecutor, SlotTiming
from qfa.settings import LLM_RETRY_BUDGET_MULTIPLIER, OrchestratorSettings

# Reuse the doubles the orchestrator suite already ships rather than growing a
# second, drifting pair (ADR-017: service tests use the real executor over the
# existing fake driven adapters).
from .test_orchestrator import FakeAnonymizer, FakeLLMPort

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0
MAX_TOKENS = 10_000


class RedactingAnonymizer(AnonymizationPort):
    """Anonymiser that actually redacts, so a round-trip is observable.

    The shared ``FakeAnonymizer`` is a deliberate no-op, which cannot show
    that ``anonymize_records`` returns a mapping capable of restoring the
    original text.
    """

    def anonymize(self, text):
        """Replace every occurrence of ``Jane`` with a PERSON placeholder."""
        return text.replace("Jane", "<PERSON_0>"), {"<PERSON_0>": "Jane"}

    def deanonymize(self, text, mapping):
        """Substitute each placeholder in ``mapping`` back into ``text``."""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text


def _make_record(doc_id="doc-1", content="Some feedback text."):
    return FeedbackRecordModel(
        id=doc_id,
        content=content,
        metadata=FeedbackRecordMetadataModel.model_validate({}),
        url_id="",
    )


def _make_response(structured="ok"):
    return LLMResponse(
        structured=structured,
        model="fake",
        prompt_tokens=10,
        completion_tokens=5,
        cost=0.0,
    )


def _make_executor(
    llm=None,
    anonymizer=None,
    settings=None,
    llm_timeout_seconds=LLM_TIMEOUT,
    max_total_tokens=MAX_TOKENS,
):
    return LLMCallExecutor(
        llm=llm if llm is not None else FakeLLMPort(responses=[_make_response()]),
        anonymizer=anonymizer if anonymizer is not None else FakeAnonymizer(),
        settings=settings or OrchestratorSettings(),
        llm_timeout_seconds=llm_timeout_seconds,
        max_total_tokens=max_total_tokens,
    )


def _future_deadline(seconds=300):
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


def _past_deadline():
    return datetime.now(tz=UTC) - timedelta(seconds=10)


async def _complete(executor, *, llm=None, deadline=None, timing=None, semaphore=None):
    """Run one ``bounded_complete`` with the boilerplate defaulted away."""
    return await executor.bounded_complete(
        semaphore or asyncio.Semaphore(1),
        llm=llm,
        system_message="sys",
        user_message="user",
        tenant_id=TENANT_ID,
        response_model=str,
        deadline=deadline or _future_deadline(),
        timing=timing,
    )


class TestCheckDeadlineAndGetTimeout:
    def test_generous_deadline_yields_the_per_call_cap(self):
        executor = _make_executor()

        # 300s remaining / 3.0 = 100s of per-attempt budget, so the configured
        # per-call ceiling is the binding constraint.
        assert executor.check_deadline_and_get_timeout(
            _future_deadline(300)
        ) == pytest.approx(LLM_TIMEOUT)

    def test_tight_deadline_reserves_the_adapter_retry_budget(self):
        """The returned window leaves room for the adapter's internal retries.

        ``LLMPort.complete`` may retry a transient failure up to
        ``LLM_RETRY_BUDGET_MULTIPLIER`` times its timeout, so the deadline-derived
        budget is divided by the same factor. Without that division a
        fully-retried last call would overrun the request deadline.
        """
        executor = _make_executor()
        remaining = 60.0

        timeout = executor.check_deadline_and_get_timeout(_future_deadline(remaining))

        assert timeout == pytest.approx(
            remaining / LLM_RETRY_BUDGET_MULTIPLIER, abs=0.5
        )
        assert timeout < LLM_TIMEOUT

    def test_expired_deadline_raises(self):
        executor = _make_executor()

        with pytest.raises(AnalysisTimeoutError, match="Deadline exceeded"):
            executor.check_deadline_and_get_timeout(_past_deadline())

    def test_insufficient_remaining_time_raises(self):
        """A window too short for any attempt fails fast instead of calling out."""
        executor = _make_executor()

        with pytest.raises(AnalysisTimeoutError, match="Insufficient time remaining"):
            executor.check_deadline_and_get_timeout(_future_deadline(5))


class TestCheckTokenLimit:
    def test_within_budget_passes(self):
        executor = _make_executor(max_total_tokens=MAX_TOKENS)

        executor.check_token_limit("system", "user")  # no raise

    def test_over_budget_is_rejected_with_the_estimate_and_limit(self):
        settings = OrchestratorSettings()
        executor = _make_executor(settings=settings, max_total_tokens=10)
        # 10 * chars_per_token characters lands exactly on the limit, so one
        # extra chars_per_token block puts the estimate over it.
        user_message = "x" * (11 * settings.chars_per_token)

        with pytest.raises(FeedbackTooLargeError) as exc_info:
            executor.check_token_limit("", user_message)

        assert exc_info.value.limit == 10
        assert exc_info.value.estimated_tokens == 11

    def test_boundary_estimate_is_allowed(self):
        """Exactly at the limit is accepted; only *over* the limit raises."""
        settings = OrchestratorSettings()
        executor = _make_executor(settings=settings, max_total_tokens=10)

        executor.check_token_limit("", "x" * (10 * settings.chars_per_token))


class TestAnonymizeRecords:
    def test_round_trips_through_the_returned_mapping(self):
        anonymizer = RedactingAnonymizer()
        executor = _make_executor(anonymizer=anonymizer)
        records = (
            _make_record("doc-1", "Jane reported a leak."),
            _make_record("doc-2", "Jane again, plus a clinic queue."),
        )

        anonymized, mapping = executor.anonymize_records(records, anonymize=True)

        assert [r.content for r in anonymized] == [
            "<PERSON_0> reported a leak.",
            "<PERSON_0> again, plus a clinic queue.",
        ]
        # The merged mapping restores every redacted record.
        assert [anonymizer.deanonymize(r.content, mapping) for r in anonymized] == [
            r.content for r in records
        ]

    def test_leaves_metadata_and_ids_untouched(self):
        executor = _make_executor(anonymizer=RedactingAnonymizer())
        records = (_make_record("doc-1", "Jane reported a leak."),)

        anonymized, _ = executor.anonymize_records(records, anonymize=True)

        assert anonymized[0].id == "doc-1"
        assert anonymized[0].metadata == records[0].metadata

    def test_disabled_returns_the_records_unchanged(self):
        executor = _make_executor(anonymizer=RedactingAnonymizer())
        records = (_make_record("doc-1", "Jane reported a leak."),)

        anonymized, mapping = executor.anonymize_records(records, anonymize=False)

        assert anonymized is records
        assert mapping == {}


class TestAnonymizeTextAndDeanonymizeJson:
    def test_round_trips_an_assembled_message_through_a_json_response(self):
        executor = _make_executor(anonymizer=RedactingAnonymizer())

        redacted, mapping = executor.anonymize_text("Jane reported a leak.")
        restored = executor.deanonymize_json(
            f'{{"explanation": "{redacted}"}}', mapping
        )

        assert redacted == "<PERSON_0> reported a leak."
        assert json.loads(restored) == {"explanation": "Jane reported a leak."}

    def test_restored_values_are_escaped_so_the_payload_stays_valid_json(self):
        """PII containing a quote must not break the JSON it is restored into.

        ``deanonymize`` is a raw substring replace, so an unescaped value
        would terminate the string it lands in and the caller's
        ``model_validate_json`` would fail on valid model output.
        """
        executor = _make_executor(anonymizer=RedactingAnonymizer())
        mapping = {"<PERSON_0>": 'Jane "JJ" Doe\n'}

        restored = executor.deanonymize_json('{"explanation": "<PERSON_0>"}', mapping)

        assert json.loads(restored) == {"explanation": 'Jane "JJ" Doe\n'}


class TestComplete:
    @pytest.mark.asyncio
    async def test_passes_the_deadline_derived_timeout_to_the_port(self):
        fake_llm = FakeLLMPort(responses=[_make_response("done")])
        executor = _make_executor(llm=fake_llm)

        response = await executor.complete(
            system_message="sys",
            user_message="user",
            tenant_id=TENANT_ID,
            response_model=str,
            deadline=_future_deadline(),
        )

        assert response.structured == "done"
        assert fake_llm.calls[0]["timeout"] == pytest.approx(LLM_TIMEOUT)

    @pytest.mark.asyncio
    async def test_expired_deadline_raises_before_calling_the_llm(self):
        fake_llm = FakeLLMPort(responses=[_make_response()])
        executor = _make_executor(llm=fake_llm)

        with pytest.raises(AnalysisTimeoutError):
            await executor.complete(
                system_message="sys",
                user_message="user",
                tenant_id=TENANT_ID,
                response_model=str,
                deadline=_past_deadline(),
            )

        assert fake_llm.calls == []


class TestBoundedComplete:
    @pytest.mark.asyncio
    async def test_passes_the_deadline_derived_timeout_to_the_port(self):
        fake_llm = FakeLLMPort(responses=[_make_response("done")])
        executor = _make_executor(llm=fake_llm)

        response = await _complete(executor, llm=fake_llm)

        assert response.structured == "done"
        assert fake_llm.calls[0]["timeout"] == pytest.approx(LLM_TIMEOUT)

    @pytest.mark.asyncio
    async def test_defaults_to_the_executors_own_client(self):
        """Omitting ``llm`` uses the connection the executor was built over."""
        primary = FakeLLMPort(responses=[_make_response("primary")])
        executor = _make_executor(llm=primary)

        response = await _complete(executor)

        assert response.structured == "primary"
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_explicit_client_overrides_the_default(self):
        """Judge calls run on their own connection without touching the primary."""
        primary = FakeLLMPort(responses=[_make_response("primary")])
        judge = FakeLLMPort(responses=[_make_response("judge")])
        executor = _make_executor(llm=primary)

        response = await _complete(executor, llm=judge)

        assert response.structured == "judge"
        assert primary.calls == []
        assert len(judge.calls) == 1

    @pytest.mark.asyncio
    async def test_expired_deadline_raises_before_calling_the_llm(self):
        fake_llm = FakeLLMPort(responses=[_make_response()])
        executor = _make_executor(llm=fake_llm)

        with pytest.raises(AnalysisTimeoutError):
            await _complete(executor, llm=fake_llm, deadline=_past_deadline())

        assert fake_llm.calls == []

    @pytest.mark.asyncio
    async def test_deadline_that_expires_during_queue_wait_is_honoured(self):
        """The timeout is derived *after* the slot is acquired, not before.

        A call that queued behind others must not spend a window that elapsed
        while it waited — the queued call is abandoned, not issued.
        """
        fake_llm = FakeLLMPort(responses=[_make_response()])
        executor = _make_executor(llm=fake_llm)
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()

        queued = asyncio.create_task(
            _complete(
                executor,
                llm=fake_llm,
                deadline=_past_deadline(),
                semaphore=semaphore,
            )
        )
        # Let the task reach the semaphore before the slot frees up.
        await asyncio.sleep(0)
        semaphore.release()

        with pytest.raises(AnalysisTimeoutError):
            await queued
        assert fake_llm.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            LLMTimeoutError("upstream timed out"),
            LLMRateLimitError("429 from provider"),
        ],
    )
    async def test_transient_errors_propagate_without_a_second_attempt(self, error):
        """Retrying transient failures is the adapter's job, not the executor's.

        ``LiteLLMClient`` retries with backoff inside one ``complete`` call; the
        executor reserves wall-clock for that (see
        ``check_deadline_and_get_timeout``) but must not add a retry loop of its
        own, which would multiply the budget it just divided.
        """
        fake_llm = FakeLLMPort(errors=[error])
        executor = _make_executor(llm=fake_llm)

        with pytest.raises(type(error)):
            await _complete(executor, llm=fake_llm)

        assert len(fake_llm.calls) == 1

    @pytest.mark.asyncio
    async def test_non_transient_error_passes_straight_through(self):
        fake_llm = FakeLLMPort(errors=[LLMError("internal server error")])
        executor = _make_executor(llm=fake_llm)

        with pytest.raises(LLMError, match="internal server error"):
            await _complete(executor, llm=fake_llm)

        assert len(fake_llm.calls) == 1

    @pytest.mark.asyncio
    async def test_semaphore_bounds_concurrent_calls(self):
        """``cap=1`` serialises the calls; nothing runs two at once."""
        in_flight = 0
        peak = 0

        class CountingLLM(FakeLLMPort):
            async def complete(self, *args, **kwargs):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    await asyncio.sleep(0)
                    return await super().complete(*args, **kwargs)
                finally:
                    in_flight -= 1

        fake_llm = CountingLLM(responses=[_make_response() for _ in range(4)])
        executor = _make_executor(llm=fake_llm)
        semaphore = asyncio.Semaphore(1)

        await asyncio.gather(
            *(_complete(executor, llm=fake_llm, semaphore=semaphore) for _ in range(4))
        )

        assert peak == 1
        assert len(fake_llm.calls) == 4

    @pytest.mark.asyncio
    async def test_timing_separates_queue_wait_from_call_time(self):
        """Queue-wait is reported apart from the call, so logs stay honest."""
        fake_llm = FakeLLMPort(responses=[_make_response()])
        executor = _make_executor(llm=fake_llm)
        semaphore = asyncio.Semaphore(1)
        timing = SlotTiming()
        await semaphore.acquire()

        queued = asyncio.create_task(
            _complete(executor, llm=fake_llm, timing=timing, semaphore=semaphore)
        )
        await asyncio.sleep(0.02)
        semaphore.release()
        await queued

        assert timing.queued_seconds >= 0.02
        assert timing.call_seconds < timing.queued_seconds

    @pytest.mark.asyncio
    async def test_timing_is_populated_even_when_the_call_fails(self):
        fake_llm = FakeLLMPort(errors=[LLMError("boom")])
        executor = _make_executor(llm=fake_llm)
        timing = SlotTiming()

        with pytest.raises(LLMError):
            await _complete(executor, llm=fake_llm, timing=timing)

        assert timing.call_seconds > 0.0
