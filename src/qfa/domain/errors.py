"""Domain error hierarchy for the feedback analysis backend."""


class DomainError(Exception):
    """Base error for all domain-level exceptions."""


# --- Orchestrator errors ---


class AnalysisError(DomainError):
    """Non-recoverable error during feedback analysis."""


class AnalysisTimeoutError(AnalysisError):
    """Raised when an analysis exceeds the allowed deadline."""


class FeedbackTooLargeError(AnalysisError):
    """Raised when estimated tokens for the submitted feedback exceed the limit.

    Attributes
    ----------
    estimated_tokens : int
        The estimated token count for the submitted feedback records.
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


class MissingRequestScopeError(RuntimeError):
    """Raised when ``call_scope`` is entered with no ambient request_id and no explicit ``call_id``.

    Indicates a wiring bug: an orchestrator entry happened outside an
    HTTP request (where ``RequestIdMiddleware`` would set
    ``current_request_id``) and without the caller passing an explicit
    ``call_id``. Tests and non-HTTP entry points must either wrap their
    call in ``request_id_scope(uuid4())`` or pass ``call_id=`` to
    ``call_scope`` directly. Should never reach a user.
    """


# --- Repository errors ---


class UsageRepositoryUnavailableError(DomainError):
    """Raised when a usage-repository read fails due to backend unavailability.

    This signals that the repository is wired and the request hit the DB
    but the connection or query failed transiently (e.g. Postgres
    unreachable, pool exhausted, broker reset). The API surfaces this as
    ``503 {"code": "usage_backend_unavailable"}``.
    """
