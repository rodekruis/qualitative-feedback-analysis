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
    AnalysisRequest,
    AnalysisResult,
    CallContext,
    CallStatus,
    FeedbackItem,
    LLMResponse,
    Operation,
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
    "CallContext",
    "CallStatus",
    "DocumentsTooLargeError",
    "DomainError",
    "FeedbackItem",
    "LLMError",
    "LLMPort",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTimeoutError",
    "MissingCallScopeError",
    "Operation",
    "OrchestratorPort",
    "TenantApiKey",
]
