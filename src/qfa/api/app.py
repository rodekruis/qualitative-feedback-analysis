"""Application factory and composition root."""

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import qfa
from qfa.adapters.db import (
    SQLAlchemyAuthAdapter,
    create_async_engine_from_settings,
    create_session_factory,
)
from qfa.adapters.env_auth import EnvironmentAuthLookupAdapter
from qfa.adapters.llm_client import LiteLLMClient
from qfa.adapters.tracking_llm import TrackingLLMAdapter
from qfa.adapters.usage_repository import SqlAlchemyUsageRepository
from qfa.api.composition import (
    build_embedder,
    build_services,
    resolve_judge_llm_settings,
)
from qfa.api.routes import router
from qfa.api.routes_admin import router as auth_router
from qfa.api.routes_usage import router as usage_router
from qfa.api.schemas import (
    ApiErrorDetail,
    ApiErrorFieldDetail,
    ApiErrorResponse,
)
from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    AuthenticationError,
    AuthorizationError,
    DomainError,
    FeedbackTooLargeError,
    KeyAlreadyExistsError,
    KeyNotFoundError,
    LLMContentPolicyViolationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    PromptInjectionDetectedError,
    TenantDoesNotAllowSuperUsersError,
    TenantNotFoundError,
    UsageRepositoryUnavailableError,
)
from qfa.domain.ports import LLMPort
from qfa.services.auth_orchestrator import AuthOrchestrator
from qfa.settings import AppSettings, LLMSettings
from qfa.utils import setup_logging

logger = logging.getLogger(__name__)

RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS = 30
"""Used only when the provider sent no usable ``Retry-After`` header.

Not a setting (ADR-018 keeps this off an env var): ~3x the adapter's
``wait_exponential(max=10)`` backoff cap, long enough to outlast a burst
the internal retry budget already failed to ride out.
"""


class RequestIdMiddleware:
    """Pure ASGI middleware that assigns a unique request ID to every request.

    Generates a fresh ``uuid4()`` per request and surfaces it two ways:

    * ``X-Request-ID`` response header — canonical UUID string format.
    * ``scope["state"]["request_id"]`` — the same string, for logging,
      error envelopes, and downstream FastAPI dependencies. The
      :func:`~qfa.api.dependencies.call_scope_for` dep reads it from
      ``request.state.request_id`` and passes it into ``call_scope`` as
      ``request_id``, so the header, logs, and ``llm_calls.call_id``
      rows always share one UUID.

    Parameters
    ----------
    app : ASGIApp
        The wrapped ASGI application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process an ASGI request.

        Assigns a unique request ID, adds it to the response headers,
        and catches any unhandled exceptions to return a 500 JSON response.

        Parameters
        ----------
        scope : Scope
            The ASGI connection scope.
        receive : Receive
            The ASGI receive callable.
        send : Send
            The ASGI send callable.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id_str = str(uuid4())
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id_str
        scope["state"]["start_utc"] = datetime.now(UTC)

        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers: list[Any] = list(message.get("headers", []))
                headers.append([b"x-request-id", request_id_str.encode()])
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            if response_started:
                raise
            logger.exception("Unhandled exception for request %s", request_id_str)
            body = ApiErrorResponse(
                error=ApiErrorDetail(
                    code="internal_error",
                    message="An unexpected error occurred",
                    request_id=request_id_str,
                )
            )
            response = JSONResponse(status_code=500, content=body.model_dump())
            response.headers["X-Request-ID"] = request_id_str
            await response(scope, receive, send)


class RequestLoggingMiddleware:
    """Pure ASGI middleware that logs every HTTP request.

    Logs method, path, status code, duration, request ID, and tenant name
    (when available). Never logs API keys or request bodies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Log method, path, status, duration, request ID, and tenant."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.get("state", {})
        request_id = state.get("request_id", "unknown")
        start = state.get("start_utc") or datetime.now(UTC)

        method = scope.get("method", "?")
        path = scope.get("path", "?")

        status_code: int | None = None

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            duration_ms = (datetime.now(UTC) - start).total_seconds() * 1000

            tenant_name = await self._resolve_tenant(scope)

            logger.info(
                "%s %s status=%s duration=%.0fms request_id=%s tenant=%s",
                method,
                path,
                status_code,
                duration_ms,
                request_id,
                tenant_name,
            )

    @staticmethod
    async def _resolve_tenant(scope: Scope) -> str:
        """Extract tenant name from the Authorization header if possible.

        Never logs the API key itself. Returns ``"anonymous"`` when the
        tenant cannot be determined.
        """
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        token: str | None = None
        for name, value in headers:
            if name.lower() == b"authorization":
                decoded = value.decode("latin-1", errors="replace")
                if decoded.lower().startswith("bearer "):
                    token = decoded[7:]
                break

        if token is None:
            return "anonymous"

        app = scope.get("app")
        if app is None:
            return "anonymous"

        try:
            tenant = await app.state.auth_orchestrator.validate_api_key(token)
            return tenant.name
        except Exception:
            return "invalid"


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state, with a fallback.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.

    Returns
    -------
    str
        The request ID string.
    """
    return getattr(request.state, "request_id", "unknown")


async def _handle_authentication_error(
    request: Request, exc: AuthenticationError
) -> JSONResponse:
    """Handle AuthenticationError exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : AuthenticationError
        The authentication error.

    Returns
    -------
    JSONResponse
        A 401 JSON response.
    """
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="authentication_required",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=401, content=body.model_dump())


async def _handle_authorization_error(
    request: Request, exc: AuthorizationError
) -> JSONResponse:
    """Handle AuthorizationError exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : AuthorizationError
        The authorization error.

    Returns
    -------
    JSONResponse
        A 403 JSON response.
    """
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="forbidden",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=403, content=body.model_dump())


async def _handle_conflict_error(request: Request, exc: DomainError) -> JSONResponse:
    """Handle conflict domain errors as HTTP 409 responses."""
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="conflict",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=409, content=body.model_dump())


async def _handle_not_found_error(request: Request, exc: DomainError) -> JSONResponse:
    """Handle missing-resource domain errors as HTTP 404 responses."""
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="not_found",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=404, content=body.model_dump())


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic RequestValidationError exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : RequestValidationError
        The validation error.

    Returns
    -------
    JSONResponse
        A 422 JSON response with per-field details.
    """
    fields = []
    for err in exc.errors():
        loc_parts = [str(part) for part in err.get("loc", [])]
        field_name = ".".join(loc_parts) if loc_parts else "unknown"
        fields.append(ApiErrorFieldDetail(field=field_name, issue=err.get("msg", "")))

    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="validation_error",
            message="Request validation failed",
            request_id=_get_request_id(request),
            fields=fields,
        )
    )
    return JSONResponse(status_code=422, content=body.model_dump())


async def _handle_feedback_too_large(
    request: Request, exc: FeedbackTooLargeError
) -> JSONResponse:
    """Handle FeedbackTooLargeError exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : FeedbackTooLargeError
        The feedback-too-large error.

    Returns
    -------
    JSONResponse
        A 413 JSON response.
    """
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="payload_too_large",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=413, content=body.model_dump())


async def _handle_analysis_timeout(
    request: Request, exc: AnalysisTimeoutError
) -> JSONResponse:
    """Handle AnalysisTimeoutError exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : AnalysisTimeoutError
        The analysis timeout error.

    Returns
    -------
    JSONResponse
        A 504 JSON response.
    """
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="analysis_timeout",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=504, content=body.model_dump())


async def _handle_analysis_error(request: Request, exc: AnalysisError) -> JSONResponse:
    """Handle AnalysisError exceptions as 502 analysis_unavailable.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : AnalysisError
        The analysis error.

    Returns
    -------
    JSONResponse
        A 502 JSON response.
    """
    logger.debug("Analysis error: %s", exc, exc_info=True)

    # Echoing str(exc) is safe only because every AnalysisError /
    # AnalysisTimeoutError message is authored in this repo as a literal
    # (or a literal plus a formatted float) — never third-party text
    # (ADR-018).
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="analysis_unavailable",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=502, content=body.model_dump())


async def _handle_prompt_injection_detected(
    request: Request, exc: PromptInjectionDetectedError
) -> JSONResponse:
    """Map a detected prompt-injection pattern to 422 prompt_injection_detected.

    The response message is a constant — the pattern name in ``str(exc)``
    is diagnostic detail for the logs only (ADR-018).
    """
    logger.debug("Prompt injection detected: %s", exc)
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="prompt_injection_detected",
            message="Input rejected: matched a known prompt-injection pattern",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=422, content=body.model_dump())


async def _handle_content_policy_violation(
    request: Request, exc: LLMContentPolicyViolationError
) -> JSONResponse:
    """Map an LLM content-policy rejection to 422 content_policy_violation.

    Distinct from other LLM failures because the request itself, not the
    provider, is at fault — the caller should not retry unmodified input.
    The response message is a constant, never ``str(exc)`` (ADR-018).
    ``category``/``severity`` are logged too when Azure's content-filter
    annotation supplied them — classified scalars, not provider text.
    """
    logger.warning(
        "LLM provider error: type=%s status=%s category=%s severity=%s",
        type(exc).__name__,
        exc.provider_status,
        exc.category,
        exc.severity,
        exc_info=True,
    )
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="content_policy_violation",
            message="LLM provider rejected the request under its content policy",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=422, content=body.model_dump())


async def _handle_llm_rate_limited(
    request: Request, exc: LLMRateLimitError
) -> JSONResponse:
    """Map an exhausted LLM rate-limit retry budget to 429 llm_rate_limited.

    Sets ``Retry-After`` from the provider's header when available
    (clamped to ``[1, 3600]``), else :data:`RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS`.
    The response message is a constant, never ``str(exc)`` (ADR-018).
    """
    logger.warning(
        "LLM provider error: type=%s status=%s",
        type(exc).__name__,
        exc.provider_status,
        exc_info=True,
    )
    retry_after = (
        max(1, min(exc.retry_after, 3600))
        if exc.retry_after is not None and exc.retry_after > 0
        else RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS
    )
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="llm_rate_limited",
            message="LLM provider rate limit exceeded",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(
        status_code=429,
        content=body.model_dump(),
        headers={"Retry-After": str(retry_after)},
    )


async def _handle_llm_timeout(request: Request, exc: LLMTimeoutError) -> JSONResponse:
    """Map an exhausted LLM timeout retry budget to 504 llm_timeout.

    The response message is a constant, never ``str(exc)`` (ADR-018).
    """
    logger.warning(
        "LLM provider error: type=%s status=%s",
        type(exc).__name__,
        exc.provider_status,
        exc_info=True,
    )
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="llm_timeout",
            message="LLM provider timed out",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=504, content=body.model_dump())


async def _handle_llm_error(request: Request, exc: LLMError) -> JSONResponse:
    """Map an LLM provider failure to 502 bad_gateway.

    LLMError signals that an upstream LLM provider call failed in a way
    the calling service did not recover from. From the API consumer's
    perspective this is a bad gateway, distinct from a 504 timeout
    (AnalysisTimeoutError) or a 502 analysis failure (AnalysisError).
    Catches ``LLMBadRequestError`` too, via MRO fall-through — it has no
    handler of its own. The response message is a constant, never
    ``str(exc)`` (ADR-018).
    """
    logger.warning(
        "LLM provider error: type=%s status=%s",
        type(exc).__name__,
        exc.provider_status,
        exc_info=True,
    )
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="llm_error",
            message="LLM provider call failed",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=502, content=body.model_dump())


async def _handle_usage_repository_unavailable(
    request: Request, exc: UsageRepositoryUnavailableError
) -> JSONResponse:
    """Map a usage-repository unavailability to 503 with a machine-readable code.

    Signals that the backing store is transiently unreachable. Consumers
    can use the code to drive retry/backoff decisions.
    """
    logger.warning("Usage repository unavailable: error_class=%s", type(exc).__name__)
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="usage_backend_unavailable",
            message="Usage backend is temporarily unavailable",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=503, content=body.model_dump())


async def _handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Wrap HTTPException with the standard error envelope.

    When ``detail`` is a dict with ``code``/``message`` keys, those are
    surfaced. Otherwise the detail string becomes the message and a
    generic ``http_error`` code is used.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        body = ApiErrorResponse(
            error=ApiErrorDetail(
                code=str(detail.get("code", "http_error")),
                message=str(detail.get("message", "")),
                request_id=_get_request_id(request),
            )
        )
    else:
        body = ApiErrorResponse(
            error=ApiErrorDetail(
                code="http_error",
                message=str(detail) if detail is not None else "",
                request_id=_get_request_id(request),
            )
        )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : Exception
        The unhandled exception.

    Returns
    -------
    JSONResponse
        A 500 JSON response.
    """
    logger.exception("Unhandled exception: %s", exc)
    body = ApiErrorResponse(
        error=ApiErrorDetail(
            code="internal_error",
            message="An unexpected error occurred",
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=500, content=body.model_dump())


def build_llm_client(settings: LLMSettings) -> LiteLLMClient:
    """Build an LLM client from the provided settings.

    Parameters
    ----------
    settings : LLMSettings
        The LLM configuration settings.

    Returns
    -------
    LiteLLMClient
        A configured LLM client instance.
    """
    return LiteLLMClient(
        model=settings.model,
        api_key=settings.api_key.get_secret_value(),
        api_base=settings.api_base,
        api_version=settings.api_version,
        chars_per_token=settings.chars_per_token,
        max_total_tokens=settings.max_total_tokens,
    )


LLMFactory = Callable[[LLMSettings], LLMPort]
"""Factory that builds an ``LLMPort`` from settings.

The default is ``build_llm_client`` (real LiteLLM client). Tests can pass
their own factory to ``create_app`` to inject a fake without monkeypatching.
"""


def _make_lifespan(llm_factory: LLMFactory):
    """Build a FastAPI lifespan context manager that closes over ``llm_factory``.

    FastAPI's ``lifespan=`` parameter accepts a single async context
    manager whose signature is fixed at ``(app: FastAPI) -> ...``. There
    is no built-in way to thread extra construction-time dependencies
    (like which ``LLMPort`` factory to use) through that signature
    without resorting to module-level globals or monkeypatching.

    This factory closes over ``llm_factory`` and returns the resulting
    lifespan, so ``create_app`` can pass a fake factory in tests and the
    lifespan picks it up at startup — wiring the same composition path
    (``llm_factory(settings.llm)`` → optional ``TrackingLLMAdapter`` wrap
    → :func:`qfa.api.composition.build_services`) regardless of whether
    the LLM client is real or stubbed. Production simply omits the
    override and gets the default ``build_llm_client``.

    Parameters
    ----------
    llm_factory : LLMFactory
        Factory invoked at startup to construct the inner ``LLMPort``.

    Returns
    -------
    Callable[[FastAPI], AsyncContextManager[None]]
        A lifespan suitable for ``FastAPI(lifespan=...)``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Compose the application graph at startup; tear it down on shutdown.

        This is the runtime composition root: it loads settings, builds
        every dependency that routes consume, and attaches the results
        to ``app.state`` so request handlers can read them without
        importing modules directly. Doing this in the lifespan (rather
        than at import time) ensures settings/env-vars are read once per
        process boot and the DB engine is created on the running event
        loop.

        Schema migrations are NOT run from the lifespan. They run as a
        pre-start step in ``entrypoint.sh`` (``python -m
        qfa.cli.migrate``) before this process binds the port, so the
        app boots against an already-current schema.

        Startup order is significant:

        1. Load ``AppSettings`` and configure logging — must happen
           before anything that might log.
        2. Build the base ``LLMPort`` via the closed-over factory, plus a
           second one for judge calls when ``JUDGE_LLM_MODEL`` is set.
        3. Create the async DB engine and wrap *both* base LLMs in
           ``TrackingLLMAdapter`` so every call attempt is recorded —
           an unwrapped judge client would omit judge calls from usage.
        4. Build the embedder here (rather than inside
           ``build_services``) so its construction is visible in
           startup logs before any traffic arrives.
        5. Delegate to :func:`qfa.api.composition.build_services`
           to assemble the application services over one shared
           ``LLMCallExecutor`` — it also registers custom LiteLLM model
           prices needed for ``completion_cost()``.
        6. Publish each service (``sensitivity_service``, ``coding_service``,
           ``analyze_service``, ``summarize_service``) plus ``api_keys``,
           ``settings``, and ``usage_repo`` on ``app.state`` for
           routes/middleware to read.

        On shutdown the only resource that needs explicit cleanup is the
        DB engine's connection pool; everything else is plain Python
        objects that the GC handles.

        Parameters
        ----------
        app : FastAPI
            The application instance whose ``state`` will be populated.
        """
        settings = AppSettings()
        setup_logging(settings.log)

        api_keys = settings.auth.api_keys

        base_llm = llm_factory(settings.llm)

        # A judge connection is optional: unset JUDGE_LLM_MODEL resolves to
        # None and judge calls stay on the primary client.
        judge_settings = resolve_judge_llm_settings(settings.llm, settings.judge_llm)
        base_judge_llm = (
            llm_factory(judge_settings) if judge_settings is not None else None
        )

        engine = create_async_engine_from_settings(settings.db)
        session_factory = create_session_factory(engine)
        usage_repo = SqlAlchemyUsageRepository(session_factory)
        auth_adapter = SQLAlchemyAuthAdapter(session_factory)
        tracked_llm: LLMPort = TrackingLLMAdapter(inner=base_llm, usage_repo=usage_repo)
        # The judge client is wrapped identically — an unwrapped one would
        # silently drop every judge call from usage and cost accounting.
        tracked_judge_llm: LLMPort | None = (
            TrackingLLMAdapter(inner=base_judge_llm, usage_repo=usage_repo)
            if base_judge_llm is not None
            else None
        )
        logger.info("Usage tracking enabled (per-attempt, per-operation)")
        if judge_settings is not None:
            logger.info(
                "Judge calls use a separate LLM connection (model=%s)",
                judge_settings.model,
            )

        # Build the embedder here (not inside the factory) so we can log
        # its construction at startup — operators rely on these lines to
        # confirm hierarchical mode is available before any traffic hits.
        if settings.embedding.model_path:
            logger.info(
                "Loading embedding model from %s ...", settings.embedding.model_path
            )
        embedder = build_embedder(settings.embedding)
        if embedder is not None:
            logger.info("Embedding model ready (hierarchical analysis available)")

        services = build_services(
            settings,
            llm=tracked_llm,
            judge_llm=tracked_judge_llm,
            embedder=embedder,
        )

        app.state.auth_orchestrator = AuthOrchestrator(
            auth_lookup_ports=[
                EnvironmentAuthLookupAdapter(api_keys=api_keys),
                auth_adapter,
            ],
            auth_management_port=auth_adapter,
        )
        # One provider per use-case service (ADR-017): each route reads the
        # single service it needs off app.state.
        app.state.sensitivity_service = services.sensitivity
        app.state.coding_service = services.coding
        app.state.analyze_service = services.analyze
        app.state.summarize_service = services.summarize
        app.state.settings = settings
        app.state.usage_repo = usage_repo

        yield

        await engine.dispose()

    return lifespan


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the application.

    Parameters
    ----------
    app : FastAPI
        The FastAPI application instance.
    """
    app.add_exception_handler(AuthorizationError, _handle_authorization_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(AuthenticationError, _handle_authentication_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(KeyAlreadyExistsError, _handle_conflict_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(KeyNotFoundError, _handle_not_found_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(TenantNotFoundError, _handle_not_found_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(
        TenantDoesNotAllowSuperUsersError,
        _handle_authorization_error,  # ty: ignore[invalid-argument-type]
    )
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(FeedbackTooLargeError, _handle_feedback_too_large)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(AnalysisTimeoutError, _handle_analysis_timeout)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(
        PromptInjectionDetectedError,
        _handle_prompt_injection_detected,  # ty: ignore[invalid-argument-type]
    )
    app.add_exception_handler(AnalysisError, _handle_analysis_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(
        LLMContentPolicyViolationError,
        _handle_content_policy_violation,  # ty: ignore[invalid-argument-type]
    )
    app.add_exception_handler(
        LLMRateLimitError,
        _handle_llm_rate_limited,  # ty: ignore[invalid-argument-type]
    )
    app.add_exception_handler(LLMTimeoutError, _handle_llm_timeout)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(LLMError, _handle_llm_error)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(
        UsageRepositoryUnavailableError,
        _handle_usage_repository_unavailable,  # ty: ignore[invalid-argument-type]
    )
    app.add_exception_handler(HTTPException, _handle_http_exception)  # ty: ignore[invalid-argument-type]
    app.add_exception_handler(Exception, _handle_unhandled_exception)


def create_app(*, llm_factory: LLMFactory | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Parameters
    ----------
    llm_factory : LLMFactory | None
        Optional override for the LLM-port factory. Defaults to
        ``build_llm_client`` (the real LiteLLM client). Tests pass a fake
        factory here to inject a stubbed ``LLMPort`` without monkeypatching
        — the lifespan still wraps it in ``TrackingLLMAdapter`` exactly as
        it would the real client.

    Returns
    -------
    FastAPI
        The fully configured application instance.
    """
    factory: LLMFactory = llm_factory if llm_factory is not None else build_llm_client

    tags_metadata = [
        {
            "name": "Default",
            "description": "System health and status endpoints",
        },
        {
            "name": "Bulk Inference",
            "description": "Batch inference endpoints that return one aggregate result",
        },
        {
            "name": "Inference",
            "description": "Non-bulk inference endpoints intended for per-feedback-record outputs",
        },
        {
            "name": "User Management",
            "description": "Manage tenants and API keys",
        },
        {
            "name": "Usage Tracking",
            "description": "View usage statistics and billing information",
        },
    ]

    app = FastAPI(
        title="Feedback Analysis Backend",
        lifespan=_make_lifespan(factory),
        version=qfa.__version__,
        openapi_tags=tags_metadata,
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(usage_router)
    register_exception_handlers(app)
    return app
