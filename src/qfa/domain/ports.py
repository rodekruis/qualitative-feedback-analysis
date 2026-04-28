"""Port interfaces (protocols) for the feedback analysis backend.

Both ports use ``typing.Protocol`` for structural subtyping per ADR-002.
"""

from datetime import datetime
from typing import Protocol

from qfa.domain.models import (
    AggregateSummaryResult,
    AnalysisRequest,
    AnalysisResult,
    CodingAssignmentRequest,
    CodingAssignmentResult,
    LLMCallRecord,
    LLMResponse,
    SummaryRequest,
    SummaryResult,
    UsageStats,
)


class LLMPort(Protocol):
    """Port for interacting with a large-language-model provider.

    Implementations must translate provider-specific details into the
    domain ``LLMResponse`` model.
    """

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ) -> LLMResponse:
        """Send a completion request to the LLM provider.

        Parameters
        ----------
        system_message : str
            The system-level instruction for the model.
        user_message : str
            The user-level message to complete.
        timeout : float
            Maximum time in seconds to wait for a response.
        tenant_id : str
            Tenant identifier for tracking and billing.

        Returns
        -------
        LLMResponse
            The model's response including token usage.
        """
        ...


class UsageRepositoryPort(Protocol):
    """Port for recording and querying LLM usage data."""

    async def record_call(self, record: LLMCallRecord) -> None:
        """Record a single LLM call.

        Parameters
        ----------
        record : LLMCallRecord
            The call record to persist.
        """
        ...

    async def get_usage_stats(self, tenant_id: str) -> UsageStats | None:
        """Get aggregated usage stats for a single tenant.

        Parameters
        ----------
        tenant_id : str
            The tenant to query.

        Returns
        -------
        UsageStats | None
            Stats for the tenant, or None if no calls recorded.
        """
        ...

    async def get_all_usage_stats(self) -> list[UsageStats]:
        """Get per-tenant stats plus a grand total entry (tenant_id=None).

        Returns
        -------
        list[UsageStats]
            Per-tenant stats followed by a grand total entry.
        """
        ...


class OrchestratorPort(Protocol):
    """Port for the analysis orchestration service.

    Even with a single implementation this port is kept explicit per ADR-008
    so that the API layer depends only on the abstraction.

    Contract
    --------
    - Raises ``AnalysisTimeoutError`` when *deadline* is exceeded.
    - Raises ``DocumentsTooLargeError`` when estimated tokens exceed the limit.
    - Raises ``AnalysisError`` for non-recoverable LLM failures.
    - Never returns partial results.
    """

    async def analyze(
        self,
        request: AnalysisRequest,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResult:
        """Analyze a batch of feedback documents.

        Parameters
        ----------
        request : AnalysisRequest
            The analysis request containing documents and prompt.
        deadline : datetime
            Absolute deadline by which the analysis must complete.
        anonymize : bool
            Whether to apply anonymization to the feedback text before analysis.

        Returns
        -------
        AnalysisResult
            The complete analysis result.

        Raises
        ------
        AnalysisTimeoutError
            When the deadline is exceeded.
        DocumentsTooLargeError
            When estimated tokens for documents exceed the configured limit.
        AnalysisError
            For non-recoverable LLM failures.
        """
        ...

    async def summarize(
        self,
        request: SummaryRequest,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SummaryResult:
        """Summarize each submitted feedback item individually.

        Parameters
        ----------
        request : SummaryRequest
            The summarization request containing feedback items and options.
        deadline : datetime
            Absolute deadline by which summarization must complete.
        anonymize : bool
            Whether to apply anonymization to the feedback text before summarization.

        Returns
        -------
        SummaryResult
            Per-feedback-item summaries and titles.
        """
        ...

    async def summarize_aggregate(
        self,
        request: SummaryRequest,
        deadline: datetime,
    ) -> AggregateSummaryResult:
        """Summarize multiple feedback items as a single aggregate summary.

        Parameters
        ----------
        request : SummaryRequest
            The summarization request containing feedback items and options.
        deadline : datetime
            Absolute deadline by which summarization must complete.

        Returns
        -------
        AggregateSummaryResult
            A single aggregate summary with themes ordered by frequency.
        """
        ...

    async def assign_codes(
        self,
        request: CodingAssignmentRequest,
        deadline: datetime,
    ) -> CodingAssignmentResult:
        """Assign hierarchical codes to each feedback item using the LLM.

        Parameters
        ----------
        request : CodingAssignmentRequest
            Items to code, framework payload, limits, and tenant id.
        deadline : datetime
            Absolute UTC deadline by which coding must complete.

        Returns
        -------
        CodingAssignmentResult
            Per-feedback-item assigned leaf codes.

        Raises
        ------
        AnalysisTimeoutError
            When the deadline is exceeded before finishing all items.
        LLMTimeoutError
            When an LLM call exceeds its per-request timeout.
        LLMRateLimitError
            When the LLM provider rate-limits a call.
        LLMError
            For other LLM provider failures.
        """
        ...
