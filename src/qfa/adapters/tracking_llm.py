"""LLM port decorator that records every call attempt for usage tracking."""

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
    ``LLMCallRecord`` per attempt (success or failure). Recording errors
    are logged but never raised, so a misbehaving usage repository never
    breaks an analysis.

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
            When ``current_call_context`` is unset; indicates a wiring bug
            (the orchestrator forgot to enter ``call_scope``).
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
    """Convert a float cost to a non-negative Decimal; coerce NaN/None to 0."""
    if cost is None:
        return Decimal("0")
    if cost != cost:
        return Decimal("0")
    if cost < 0:
        return Decimal("0")
    return Decimal(repr(cost))
