"""Domain error hierarchy for the feedback analysis backend."""


class DomainError(Exception):
    """Base error for all domain-level exceptions."""


# --- Orchestrator errors ---


class AnalysisError(DomainError):
    """Non-recoverable error during feedback analysis."""


class AnalysisTimeoutError(AnalysisError):
    """Raised when an analysis exceeds the allowed deadline."""


class DocumentsTooLargeError(AnalysisError):
    """Raised when estimated tokens for documents exceed the limit.

    Attributes
    ----------
    estimated_tokens : int
        The estimated token count for the submitted documents.
    limit : int
        The maximum allowed token count.
    """

    def __init__(self, message: str, *, estimated_tokens: int, limit: int) -> None:
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.limit = limit


# --- LLM adapter errors ---


class LLMError(DomainError):
    """Base error for LLM adapter failures."""


class LLMTimeoutError(LLMError):
    """Raised when the LLM provider does not respond in time."""


class LLMRateLimitError(LLMError):
    """Raised when the LLM provider returns a rate-limit response."""


# --- Auth errors ---


class AuthenticationError(DomainError):
    """Raised when an API request cannot be authenticated."""


class AuthorizationError(DomainError):
    """Raised when a user lacks permission for the requested operation."""


# --- Tracking errors ---


class MissingCallScopeError(RuntimeError):
    """Raised when an LLM call is recorded without an active CallContext.

    Indicates a wiring bug: the orchestrator forgot to enter a ``call_scope``
    block before calling the LLM. Should never reach a user.
    """


# --- Repository errors ---


class UsageRepositoryUnavailableError(DomainError):
    """Raised when a usage-repository read fails due to backend unavailability.

    Distinct from "feature disabled": this signals that the repository is
    wired and the request hit the DB but the connection or query failed
    transiently (e.g. Postgres unreachable, pool exhausted, broker reset).
    The API surfaces this as ``503 {"code": "usage_backend_unavailable"}``,
    so consumers can distinguish it from ``usage_tracking_disabled`` and
    drive retry/backoff logic accordingly.
    """
