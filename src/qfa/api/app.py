"""Application factory and composition root."""

import importlib.resources
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import litellm
import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import qfa
from qfa.adapters.llm_client import LiteLLMClient
from qfa.api.routes import router
from qfa.api.schemas import (
    ErrorDetail,
    ErrorFieldDetail,
    ErrorResponse,
)
from qfa.auth import validate_api_key
from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    AuthenticationError,
    DocumentsTooLargeError,
)
from qfa.services.orchestrator import StandardOrchestrator
from qfa.settings import AppSettings, LLMSettings
from qfa.utils import setup_logging

logger = logging.getLogger(__name__)


class RequestIdMiddleware:
    """Pure ASGI middleware that assigns a unique request ID to every request.

    Stores ``request_id`` and ``start_utc`` on ``scope["state"]`` and
    adds an ``X-Request-ID`` header to every response.

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

        request_id = "req_" + secrets.token_urlsafe(16)
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        scope["state"]["start_utc"] = datetime.now(UTC)

        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers: list[Any] = list(message.get("headers", []))
                headers.append([b"x-request-id", request_id.encode()])
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            if response_started:
                raise
            logger.exception("Unhandled exception for request %s", request_id)
            body = ErrorResponse(
                error=ErrorDetail(
                    code="internal_error",
                    message="An unexpected error occurred",
                    request_id=request_id,
                )
            )
            response = JSONResponse(status_code=500, content=body.model_dump())
            response.headers["X-Request-ID"] = request_id
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

            tenant_name = self._resolve_tenant(scope)

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
    def _resolve_tenant(scope: Scope) -> str:
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

        api_keys = getattr(getattr(app, "state", None), "api_keys", None)
        if not api_keys:
            return "anonymous"

        try:
            tenant = validate_api_key(token, api_keys)
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
    body = ErrorResponse(
        error=ErrorDetail(
            code="authentication_required",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=401, content=body.model_dump())


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
        fields.append(ErrorFieldDetail(field=field_name, issue=err.get("msg", "")))

    body = ErrorResponse(
        error=ErrorDetail(
            code="validation_error",
            message="Request validation failed",
            request_id=_get_request_id(request),
            fields=fields,
        )
    )
    return JSONResponse(status_code=422, content=body.model_dump())


async def _handle_documents_too_large(
    request: Request, exc: DocumentsTooLargeError
) -> JSONResponse:
    """Handle DocumentsTooLargeError exceptions.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : DocumentsTooLargeError
        The documents too large error.

    Returns
    -------
    JSONResponse
        A 413 JSON response.
    """
    body = ErrorResponse(
        error=ErrorDetail(
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
    body = ErrorResponse(
        error=ErrorDetail(
            code="analysis_timeout",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=504, content=body.model_dump())


async def _handle_analysis_error(request: Request, exc: AnalysisError) -> JSONResponse:
    """Handle AnalysisError exceptions.

    If the error message contains "injection", returns 422 instead of 502
    to signal that the input was rejected.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : AnalysisError
        The analysis error.

    Returns
    -------
    JSONResponse
        A 502 or 422 JSON response depending on the error cause.
    """
    logger.debug("Analysis error: %s", exc, exc_info=True)

    if "injection" in str(exc).lower():
        body = ErrorResponse(
            error=ErrorDetail(
                code="validation_error",
                message=str(exc),
                request_id=_get_request_id(request),
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    body = ErrorResponse(
        error=ErrorDetail(
            code="analysis_unavailable",
            message=str(exc),
            request_id=_get_request_id(request),
        )
    )
    return JSONResponse(status_code=502, content=body.model_dump())


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
    body = ErrorResponse(
        error=ErrorDetail(
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
    )


def _register_custom_model_prices() -> None:
    """Load custom model pricing from the bundled YAML resource.

    Registers models with LiteLLM so that ``completion_cost()`` works
    for models not in the built-in cost map.
    """
    prices_path = importlib.resources.files("qfa.resources").joinpath(
        "model_prices.yaml"
    )
    with importlib.resources.as_file(prices_path) as f:
        custom_prices = yaml.safe_load(f.read_text())
    if custom_prices and custom_prices.get("models"):
        litellm.register_model(custom_prices["models"])
        logger.info(
            "Registered %d custom model price(s) for %s",
            len(custom_prices["models"]),
            list(custom_prices["models"].keys()),
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: compose and inject all dependencies.

    Parameters
    ----------
    app : FastAPI
        The FastAPI application instance.

    Yields
    ------
    None
    """
    settings = AppSettings()
    setup_logging(settings.log)

    _register_custom_model_prices()

    llm_client = build_llm_client(settings.llm)
    orchestrator = StandardOrchestrator(
        llm=llm_client,
        settings=settings.orchestrator,
        llm_timeout_seconds=settings.llm.timeout_seconds,
        max_total_tokens=settings.llm.max_total_tokens,
    )
    api_keys = settings.auth.api_keys

    app.state.orchestrator = orchestrator
    app.state.api_keys = api_keys
    app.state.settings = settings

    yield


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the application.

    Parameters
    ----------
    app : FastAPI
        The FastAPI application instance.
    """
    app.add_exception_handler(AuthenticationError, _handle_authentication_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(DocumentsTooLargeError, _handle_documents_too_large)  # type: ignore[arg-type]
    app.add_exception_handler(AnalysisTimeoutError, _handle_analysis_timeout)  # type: ignore[arg-type]
    app.add_exception_handler(AnalysisError, _handle_analysis_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unhandled_exception)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns
    -------
    FastAPI
        The fully configured application instance.
    """
    app = FastAPI(
        title="Feedback Analysis Backend",
        lifespan=lifespan,
        version=qfa.__version__,
    )
    app.add_middleware(RequestLoggingMiddleware)  # type: ignore[arg-type]
    app.add_middleware(RequestIdMiddleware)  # type: ignore[arg-type]
    app.include_router(router)
    register_exception_handlers(app)
    return app
