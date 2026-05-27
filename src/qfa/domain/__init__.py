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
)
from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    FeedbackRecordModel,
    LLMResponse,
    TenantApiKey,
)
from qfa.domain.ports import AnonymizationPort, LLMPort
from qfa.domain.usage_models import (
    CallContext,
    CallStatus,
    Operation,
)

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
    "Operation",
    "TenantApiKey",
]
