"""Domain layer — models, errors, and port interfaces."""

from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    AuthenticationError,
    DomainError,
    FeedbackTooLargeError,
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
    FeedbackRecordModel,
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
    "DomainError",
    "FeedbackRecordModel",
    "FeedbackTooLargeError",
    "LLMError",
    "LLMPort",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTimeoutError",
    "MissingCallScopeError",
    "Operation",
    "TenantApiKey",
]
