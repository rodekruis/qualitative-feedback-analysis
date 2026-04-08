"""Domain layer — models, errors, and port interfaces."""

from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    AuthenticationError,
    DocumentsTooLargeError,
    DomainError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import (
    AnalysisRequest,
    AnalysisResult,
    FeedbackItem,
    LLMResponse,
    TenantApiKey,
)
from qfa.domain.ports import LLMPort, OrchestratorPort

__all__ = [
    "AnalysisError",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisTimeoutError",
    "AuthenticationError",
    "DocumentsTooLargeError",
    "DomainError",
    "FeedbackItem",
    "LLMError",
    "LLMPort",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTimeoutError",
    "OrchestratorPort",
    "TenantApiKey",
]
