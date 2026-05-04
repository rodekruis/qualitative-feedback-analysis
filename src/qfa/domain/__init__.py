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
    AnalysisRequestModel,
    AnalysisResultModel,
    FeedbackItemModel,
    LLMResponse,
    TenantApiKey,
)
from qfa.domain.ports import AnonymizationPort, LLMPort

__all__ = [
    "AnalysisError",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisTimeoutError",
    "AnonymizationPort",
    "AuthenticationError",
    "DocumentsTooLargeError",
    "DomainError",
    "FeedbackItem",
    "LLMError",
    "LLMPort",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTimeoutError",
    "TenantApiKey",
]
