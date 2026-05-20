"""LLM port decorator that records every call attempt for usage tracking."""

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import InterfaceError, OperationalError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from qfa.domain.models import (
    CallContext,
    CallStatus,
    LLMCallRecord,
    LLMResponse,
    T_Response,
)
from qfa.domain.ports import LLMPort, UsageRepositoryPort
from qfa.services.call_context import current_call_context

logger = logging.getLogger(__name__)


class TrackingLLMAdapter(LLMPort):
    """Decorator over an inner ``LLMPort`` that records every call attempt.

    Reads tenant + operation from ``current_call_context``. Persists one
    ``LLMCallRecord`` per attempt (success or failure). Recording errors
    are logged but never raised, so a misbehaving usage repository never
    breaks an analysis. Connection-class transient errors
    (``OperationalError``, ``InterfaceError``) are retried up to 3 times
    with exponential backoff capped at 0.5s per wait — worst-case added
    latency under a sustained DB outage is ~0.3s of waits plus 3
    fast-failing connection attempts (typically <1s total). Non-transient
    errors (``IntegrityError``, ``ProgrammingError``, etc.) skip the
    retry path and are logged immediately.

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
        tenant_id: str,
        response_model: type[T_Response],
        timeout: float = 20.0,
    ) -> LLMResponse[T_Response]:
        """Run the inner ``complete`` and record the attempt.

        If ``current_call_context`` is unset the call still goes through
        — observability never breaks the use case — but the attempt is
        not persisted and the missing scope is logged at ERROR. In the
        current wiring this happens only when the orchestrator is
        invoked outside an HTTP request (e.g. a CLI or test that forgot
        to set up scopes); HTTP paths set the scope via
        ``call_scope_for`` at the route layer.
        """
        ctx = current_call_context.get()
        started_at = datetime.now(UTC)
        start_monotonic = time.monotonic()

        outcome: LLMResponse[T_Response] | Exception
        try:
            outcome = await self._inner.complete(
                system_message=system_message,
                user_message=user_message,
                tenant_id=tenant_id,
                response_model=response_model,
                timeout=timeout,
            )
        except Exception as exc:
            outcome = exc

        duration_ms = int((time.monotonic() - start_monotonic) * 1000)

        if ctx is None:
            logger.error(
                "TrackingLLMAdapter.complete called outside an active "
                "call_scope; bypassing persistence and routing through to "
                "inner LLM. This indicates a wiring bug — every entry "
                "into the orchestrator should be wrapped in call_scope "
                "(routes do this via Depends(call_scope_for(...))).",
            )
        else:
            await self._record_safely(
                _build_record(ctx, started_at, duration_ms, outcome)
            )

        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @retry(
        retry=retry_if_exception_type((OperationalError, InterfaceError)),
        wait=wait_exponential(multiplier=0.05, min=0.05, max=0.5),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _record_with_retry(self, record: LLMCallRecord) -> None:
        """Persist with bounded retries on connection-class transient errors."""
        await self._usage_repo.record_call(record)

    async def _record_safely(self, record: LLMCallRecord) -> None:
        """Try to persist to DB, log error on failure (do not raise).

        Rationale: if the usage repository is misbehaving, we don't want the endpoint
        to fail.
        """
        try:
            await self._record_with_retry(record)
        except Exception:
            logger.exception(
                "Failed to record LLM call for tenant=%s operation=%s",
                record.tenant_id,
                record.operation,
            )


def _build_record(
    ctx: CallContext,
    started_at: datetime,
    duration_ms: int,
    outcome: LLMResponse | Exception,
) -> LLMCallRecord:
    """Build an ``LLMCallRecord`` for one inner-LLM attempt.

    ``outcome`` carries either the successful ``LLMResponse`` or the
    captured exception — same shape on both paths except for the
    response/error-specific fields.
    """
    if isinstance(outcome, Exception):
        return LLMCallRecord(
            tenant_id=ctx.tenant_id,
            operation=ctx.operation,
            call_id=ctx.call_id,
            timestamp=started_at,
            call_duration_ms=duration_ms,
            model="",
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status=CallStatus.ERROR,
            error_class=type(outcome).__name__,
        )
    return LLMCallRecord(
        tenant_id=ctx.tenant_id,
        operation=ctx.operation,
        call_id=ctx.call_id,
        timestamp=started_at,
        call_duration_ms=duration_ms,
        model=outcome.model,
        input_tokens=outcome.prompt_tokens,
        output_tokens=outcome.completion_tokens,
        cost_usd=_to_decimal(outcome.cost),
        status=CallStatus.OK,
        error_class=None,
    )


def _to_decimal(cost: float | None) -> Decimal:
    """Convert a float cost to a non-negative Decimal; coerce NaN/None to 0."""
    if cost is None:
        return Decimal("0")
    if cost != cost:
        return Decimal("0")
    if cost < 0:
        return Decimal("0")
    return Decimal(repr(cost))
