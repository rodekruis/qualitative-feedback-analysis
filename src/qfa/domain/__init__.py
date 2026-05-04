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
    MissingCallScopeError,
)
from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    CallContext,
    CallStatus,
    FeedbackItemModel,
    LLMResponse,
    Operation,
    TenantApiKey,
)
from qfa.domain.ports import AnonymizationPort, LLMPort

__all__ = [
    "AnalysisError",
    "AnalysisRequestModel",
    "AnalysisResultModel",
    "AnalysisTimeoutError",
    "AnonymizationPort",
    "AuthenticationError",
    "CallContext",
    "CallStatus",
    "DocumentsTooLargeError",
    "DomainError",
    "FeedbackItemModel",
    "LLMError",
    "LLMPort",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTimeoutError",
    "MissingCallScopeError",
    "Operation",
    "TenantApiKey",
]
