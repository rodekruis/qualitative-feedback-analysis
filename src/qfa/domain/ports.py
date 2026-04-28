"""Port interfaces (protocols) for the feedback analysis backend.

Both ports use ``typing.Protocol`` for structural subtyping per ADR-002.
"""

from datetime import datetime
from typing import Protocol

from qfa.domain.models import (
    AggregateSummaryResultModel,
    AnalysisRequestModel,
    AnalysisResultModel,
    CodingAssignmentRequestModel,
    CodingAssignmentResultModel,
    LLMResponse,
    SummaryRequestModel,
    SummaryResultModel,
    T_Response,
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
        tenant_id: str,
        response_model: type[T_Response],
        anonymize: bool = True,
        timeout: float = 20.0,
    ) -> LLMResponse[T_Response]:
        """Send a completion request to the LLM provider.

        Parameters
        ----------
        system_message : str
            The system-level instruction for the model.
        user_message : str
            The user-level message to complete.
        tenant_id : str
            Tenant identifier for tracking and billing.
        response_model : type[T_Response]
            The Pydantic model to parse the response into.
        anonymize : bool
            Whether to anonymize the user message before sending.
        timeout : float
            Maximum time in seconds to wait for a response.

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
        request: AnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResultModel:
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
        request: SummaryRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SummaryResultModel:
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
        request: SummaryRequestModel,
        deadline: datetime,
    ) -> AggregateSummaryResultModel:
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
        request: CodingAssignmentRequestModel,
        deadline: datetime,
    ) -> CodingAssignmentResultModel:
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
