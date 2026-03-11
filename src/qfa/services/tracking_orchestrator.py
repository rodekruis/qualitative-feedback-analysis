"""Tracking orchestrator — decorator that records LLM usage."""

import logging
import time
from datetime import UTC, datetime

from qfa.domain.models import AnalysisRequest, AnalysisResult, LLMCallRecord
from qfa.domain.ports import OrchestratorPort, UsageRepositoryPort

logger = logging.getLogger(__name__)


class TrackingOrchestrator(OrchestratorPort):
    """Orchestrator decorator that records usage after each call.

    Delegates ``analyze()`` to the inner orchestrator, measures wall-clock
    duration, and persists a usage record. Recording failures are logged
    but never break the analysis flow.

    Parameters
    ----------
    inner : OrchestratorPort
        The wrapped orchestrator.
    usage_repo : UsageRepositoryPort
        Repository for persisting usage records.
    """

    def __init__(
        self,
        inner: OrchestratorPort,
        usage_repo: UsageRepositoryPort,
    ) -> None:
        self._inner = inner
        self._usage_repo = usage_repo

    async def analyze(
        self,
        request: AnalysisRequest,
        deadline: datetime,
    ) -> AnalysisResult:
        """Analyze documents and record usage.

        Parameters
        ----------
        request : AnalysisRequest
            The analysis request.
        deadline : datetime
            Absolute UTC deadline.

        Returns
        -------
        AnalysisResult
            The analysis result from the inner orchestrator.
        """
        start = time.monotonic()
        result = await self._inner.analyze(request, deadline)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            record = LLMCallRecord(
                tenant_id=request.tenant_id,
                timestamp=datetime.now(UTC),
                call_duration_ms=duration_ms,
                model=result.model,
                input_tokens=result.prompt_tokens,
                output_tokens=result.completion_tokens,
            )
            await self._usage_repo.record_call(record)
        except Exception:
            logger.exception("Failed to record usage for tenant %s", request.tenant_id)

        return result
