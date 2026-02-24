"""Domain layer — models, errors, and port interfaces."""

from feedback_analysis_backend.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    AuthenticationError,
    DocumentsTooLargeError,
    DomainError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from feedback_analysis_backend.domain.models import (
    AnalysisRequest,
    AnalysisResult,
    FeedbackDocument,
    LLMResponse,
    TenantApiKey,
)
from feedback_analysis_backend.domain.ports import LLMPort, OrchestratorPort

__all__ = [
    "AnalysisError",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisTimeoutError",
    "AuthenticationError",
    "DocumentsTooLargeError",
    "DomainError",
    "FeedbackDocument",
    "LLMError",
    "LLMPort",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTimeoutError",
    "OrchestratorPort",
    "TenantApiKey",
]
