"""Port interfaces (protocols) for the feedback analysis backend.

Both ports use ``typing.Protocol`` for structural subtyping per ADR-002.
"""

from datetime import datetime
from typing import Protocol

from qfa.domain.models import (
    AggregateSummaryResult,
    AnalysisRequest,
    AnalysisResult,
    LLMResponse,
    SummaryRequest,
    SummaryResult,
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
    ) -> AnalysisResult:
        """Analyze a batch of feedback documents.

        Parameters
        ----------
        request : AnalysisRequest
            The analysis request containing documents and prompt.
        deadline : datetime
            Absolute deadline by which the analysis must complete.

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
    ) -> SummaryResult:
        """Summarize each submitted feedback item individually.

        Parameters
        ----------
        request : SummaryRequest
            The summarization request containing feedback items and options.
        deadline : datetime
            Absolute deadline by which summarization must complete.

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
