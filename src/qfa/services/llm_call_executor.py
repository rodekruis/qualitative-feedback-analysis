"""Shared LLM-call scaffolding for the application services.

Every use case in :mod:`qfa.services` wraps its LLM calls in the same
four concerns: redact PII before the call, derive a per-call timeout from
the request deadline, guard the token budget, and bound concurrent calls
with a semaphore. :class:`LLMCallExecutor` owns those four and nothing
else, so a use-case service can be read without them.

Per ADR-017 this is a plain concrete class, deliberately **not** a
Protocol and **not** a base class:

- It is not a driven port. Ports in this codebase invert dependencies on
  *infrastructure*; this object wraps no external system, it orchestrates
  calls to an :class:`~qfa.domain.ports.LLMPort` that already exists. It
  is therefore not declared in :mod:`qfa.domain.ports`.
- Services receive it as a constructor dependency and delegate to it.
  Nothing inherits from it — behaviour reuse in this codebase is always
  composition, and ``class X(Y):`` stays readable as "X implements port
  Y".

Tests construct the *real* executor over the existing ``FakeLLMPort`` /
``FakeAnonymizer`` doubles; there is no fake executor.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from qfa.domain.errors import AnalysisTimeoutError, FeedbackTooLargeError
from qfa.domain.models import FeedbackRecordModel, LLMResponse, T_Response
from qfa.domain.ports import AnonymizationPort, LLMPort
from qfa.settings import LLM_RETRY_BUDGET_MULTIPLIER, OrchestratorSettings

#: Minimum time (seconds) required for an LLM attempt to be viable.
_MINIMUM_ATTEMPT_WINDOW = 10.0


@dataclass
class SlotTiming:
    """Split timing for one semaphore-bounded hierarchical LLM call.

    ``queued_seconds`` is the time spent waiting to acquire the concurrency
    semaphore; ``call_seconds`` is the LLM completion itself, measured only
    *after* the slot was acquired. Keeping them apart makes the per-chunk
    debug lines honest: a single combined duration folds queue-wait into
    "call time", so a 5s call that waited 100s in the queue looked like a
    110s call (and like a near-timeout when it was nothing of the sort).
    """

    queued_seconds: float = 0.0
    call_seconds: float = 0.0


class LLMCallExecutor:
    """Deadline-, budget- and concurrency-aware wrapper around an LLM port.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter every call runs on by default. Call sites
        that must use a *different* connection — the LLM-as-judge calls,
        which may be configured onto their own model — pass it explicitly
        to :meth:`bounded_complete`.
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before LLM calls.
    settings : OrchestratorSettings
        Cross-cutting configuration; this object reads ``chars_per_token``
        for its token estimate.
    llm_timeout_seconds : float
        Maximum time in seconds for a single LLM call, before the deadline
        is taken into account.
    max_total_tokens : int
        Maximum estimated total tokens for a single request.
    """

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        settings: OrchestratorSettings,
        llm_timeout_seconds: float,
        max_total_tokens: int,
    ) -> None:
        self._llm = llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._settings = settings
        self._llm_timeout_seconds = llm_timeout_seconds
        self._max_total_tokens = max_total_tokens

    def anonymize_records(
        self,
        records: tuple[FeedbackRecordModel, ...],
        anonymize: bool,
    ) -> tuple[tuple[FeedbackRecordModel, ...], dict[str, str]]:
        """Anonymise each record's text, returning new records + merged mapping.

        Metadata is left untouched (codes/dates are not PII and feed the
        deterministic trend table). When ``anonymize`` is False, records are
        returned unchanged with an empty mapping.
        """
        if not anonymize:
            return records, {}
        merged: dict[str, str] = {}
        new_records: list[FeedbackRecordModel] = []
        for record in records:
            redacted, mapping = self._anonymizer.anonymize(record.content)
            merged.update(mapping)
            new_records.append(record.model_copy(update={"content": redacted}))
        return tuple(new_records), merged

    async def bounded_complete(
        self,
        semaphore: asyncio.Semaphore,
        *,
        llm: LLMPort | None = None,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
        deadline: datetime,
        timing: SlotTiming | None = None,
    ) -> LLMResponse[T_Response]:
        """Run one LLM completion, bounded by ``semaphore`` and the deadline.

        ``semaphore`` caps how many completions run at once across the whole
        hierarchical pipeline (map, leaf judge, reduce), so concurrency stays
        within ``max_concurrent_chunks`` across every phase. The
        deadline/timeout is computed *after* acquiring a slot, so a
        completion that queued behind others still honours the remaining budget
        (and raises ``AnalysisTimeoutError`` if the deadline passed while it
        waited).

        ``llm`` overrides the connection for this one call. It exists because
        this helper serves both map/reduce and the leaf judges, which may run
        on different connections (see ``judge_llm``); ``None`` uses the
        executor's own client. Every current call site passes it explicitly so
        the connection each call uses is visible where the call is written.
        Note the semaphore is shared regardless: the bound is on total
        in-flight calls, not per connection.

        When ``timing`` is supplied it is populated with the queue-wait and the
        post-acquire call duration as two separate fields, so callers can log
        them apart rather than reporting one combined number that hides how long
        the call sat waiting for a slot.
        """
        client = llm if llm is not None else self._llm
        queue_start = time.perf_counter()
        async with semaphore:
            acquired_at = time.perf_counter()
            if timing is not None:
                timing.queued_seconds = acquired_at - queue_start
            # Compute the timeout only now: queue-wait already elapsed, so the
            # per-call window reflects the budget that actually remains.
            timeout = self.check_deadline_and_get_timeout(deadline)
            try:
                return await client.complete(
                    system_message=system_message,
                    user_message=user_message,
                    tenant_id=tenant_id,
                    response_model=response_model,
                    timeout=timeout,
                )
            finally:
                if timing is not None:
                    timing.call_seconds = time.perf_counter() - acquired_at

    def check_deadline_and_get_timeout(self, deadline: datetime) -> float:
        """Raise if the deadline has passed or too little time remains.

        Return a timeout (seconds) bounded by the deadline and the
        configured per-call limit.
        """
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise AnalysisTimeoutError("Deadline exceeded")
        if remaining < _MINIMUM_ATTEMPT_WINDOW:
            raise AnalysisTimeoutError(
                f"Insufficient time remaining ({remaining:.1f}s) for an LLM attempt"
            )
        # A single ``LLMPort.complete`` may retry internally, spending up to
        # ``LLM_RETRY_BUDGET_MULTIPLIER`` times this per-attempt timeout in worst
        # case (see ``qfa.adapters.llm_client``). Divide the remaining deadline
        # by the same factor so even a fully-retried last call finishes before
        # the deadline. With a generous deadline the per-call cap binds first, so
        # this only bites as the deadline approaches.
        per_attempt_budget = remaining / LLM_RETRY_BUDGET_MULTIPLIER
        return min(self._llm_timeout_seconds, per_attempt_budget)

    def check_token_limit(self, system_message: str, user_message: str) -> None:
        """Estimate total tokens and raise if over the limit.

        Parameters
        ----------
        system_message : str
            The assembled system message.
        user_message : str
            The assembled user message containing the feedback records.

        Raises
        ------
        FeedbackTooLargeError
            When estimated tokens exceed the configured limit.
        """
        assembled_text = system_message + user_message
        estimated_tokens = len(assembled_text) // self._settings.chars_per_token
        if estimated_tokens > self._max_total_tokens:
            msg = (
                f"Estimated tokens ({estimated_tokens}) exceed limit "
                f"({self._max_total_tokens})"
            )
            raise FeedbackTooLargeError(
                msg,
                estimated_tokens=estimated_tokens,
                limit=self._max_total_tokens,
            )
