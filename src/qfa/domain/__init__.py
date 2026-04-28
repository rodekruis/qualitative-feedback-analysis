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
    OperationStats,
    TenantApiKey,
)
from qfa.domain.ports import LLMPort, OrchestratorPort

__all__ = [
    "AnalysisError",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisTimeoutError",
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
    "OperationStats",
    "OrchestratorPort",
    "TenantApiKey",
]
