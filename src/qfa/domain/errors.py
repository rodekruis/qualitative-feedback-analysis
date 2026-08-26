"""Domain error hierarchy for the feedback analysis backend."""


class DomainError(Exception):
    """Base error for all domain-level exceptions."""


# --- Analysis errors ---


class AnalysisError(DomainError):
    """Non-recoverable error during feedback analysis."""


class AnalysisTimeoutError(AnalysisError):
    """Raised when an analysis exceeds the allowed deadline."""


class PromptInjectionDetectedError(AnalysisError):
    """Raised when input matches a known prompt-injection pattern.

    Sole raiser: ``LiteLLMClient._check_injection``.
    """


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
    """Base error for LLM adapter failures.

    ``provider_status`` is a classified scalar (the provider's HTTP status
    code), never free text — see ADR-018. ``None`` means the provider did
    not expose a usable status code.
    """

    def __init__(self, message: str, *, provider_status: int | None = None) -> None:
        super().__init__(message)
        self.provider_status = provider_status


class LLMTimeoutError(LLMError):
    """Raised when the LLM provider does not respond in time."""


class LLMBadRequestError(LLMError):
    """Raised when the LLM provider returns a 400 Bad Request response."""


class LLMContentPolicyViolationError(LLMBadRequestError):
    """Raised when the LLM provider rejects the request due to content policy.

    ``category`` and ``severity`` are Azure's content-filter annotation
    (e.g. ``"violence"`` / ``"high"``) when the rejection was detected from
    a completed response's ``content_filter_results`` rather than sniffed
    from a ``BadRequestError`` message — a closed, low-cardinality set of
    classified scalars, never free text (see ADR-018). Both are ``None``
    when unavailable.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_status: int | None = None,
        category: str | None = None,
        severity: str | None = None,
    ) -> None:
        super().__init__(message, provider_status=provider_status)
        self.category = category
        self.severity = severity


class LLMRateLimitError(LLMError):
    """Raised when the LLM provider returns a rate-limit response.

    ``retry_after`` is the provider's ``Retry-After`` value in **seconds**.
    ``None`` means the provider gave no usable header.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, provider_status=provider_status)
        self.retry_after = retry_after


class LLMResponseParseError(LLMError):
    """Raised when a structured LLM response fails schema validation.

    Distinct from other ``LLMError`` cases (timeouts, missing content,
    provider failures) so callers may choose to treat malformed model
    output as an empty/absent result instead of failing the request.
    """


# --- Auth errors ---


class AuthenticationError(DomainError):
    """Raised when an API request cannot be authenticated."""


class AuthorizationError(DomainError):
    """Raised when a user lacks permission for the requested operation."""


class TenantDoesNotAllowSuperUsersError(DomainError):
    """Raised when an operation requires superuser privileges but the tenant does not allow superusers."""


class KeyAlreadyExistsError(DomainError):
    """Raised when we try to create a key with an existing id."""


class KeyNotFoundError(DomainError):
    """Raised when we try to access a key that doesn't exist."""


class TenantNotFoundError(DomainError):
    """Raised when we try to access a tenant that doesn't exist."""


# --- Tracking errors ---


class MissingCallScopeError(RuntimeError):
    """Raised when an LLM call is recorded without an active CallContext.

    Indicates a wiring bug: the driving adapter forgot to enter a
    ``call_scope`` block before calling the LLM. Should never reach a user.
    """


# --- Repository errors ---


class UsageRepositoryUnavailableError(DomainError):
    """Raised when a usage-repository read fails due to backend unavailability.

    This signals that the repository is wired and the request hit the DB
    but the connection or query failed transiently (e.g. Postgres
    unreachable, pool exhausted, broker reset). The API surfaces this as
    ``503 {"code": "usage_backend_unavailable"}``.
    """
