"""FastAPI dependency functions for authentication and service injection."""

from collections.abc import AsyncIterator
from typing import Callable
from uuid import UUID

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qfa.domain.errors import AuthenticationError, AuthorizationError
from qfa.domain.models import TenantApiKey
from qfa.domain.ports import UsageRepositoryPort
from qfa.domain.usage_models import CallContext, Operation
from qfa.services.auth_orchestrator import AuthOrchestrator
from qfa.services.call_context import call_scope
from qfa.services.orchestrator import Orchestrator


def get_orchestrator(request: Request) -> Orchestrator:
    """Return the orchestrator from app state.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.

    Returns
    -------
    Orchestrator
        The orchestrator service instance.
    """
    return request.app.state.orchestrator


def get_auth_orchestrator(request: Request) -> AuthOrchestrator:
    """Return the auth orchestrator from app state.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.

    Returns
    -------
    AuthOrchestrator
        The auth orchestrator service instance.
    """
    return request.app.state.auth_orchestrator


def get_usage_repo(request: Request) -> UsageRepositoryPort:
    """Return the usage repository from app state.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.

    Returns
    -------
    UsageRepositoryPort
        The usage repository instance.
    """
    return request.app.state.usage_repo


async def authenticate_request(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(HTTPBearer(auto_error=False)),
) -> TenantApiKey:
    """Validate a Bearer token from the Authorization header.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    credentials : HTTPAuthorizationCredentials
        The parsed Authorization header credentials.

    Returns
    -------
    TenantApiKey
        The authenticated tenant API key.

    Raises
    ------
    AuthenticationError
        If the credentials are missing or invalid.
    """
    error_message = (
        "A valid API key is required. Provide it as: Authorization: Bearer <key>"
    )
    if credentials is None:
        raise AuthenticationError(error_message)

    try:
        return await request.app.state.auth_orchestrator.validate_api_key(
            credentials.credentials
        )
    except AuthenticationError:
        raise AuthenticationError(error_message)


def call_scope_for(
    operation: Operation,
) -> Callable[..., AsyncIterator[CallContext]]:
    """Build a FastAPI dependency that enters ``call_scope`` for ``operation``.

    The returned dependency reads the authenticated tenant from
    :func:`authenticate_request`, enters ``call_scope`` for the duration
    of the request, and yields the resulting ``CallContext``. It is the
    *driving adapter*'s contribution to the cross-adapter correlation
    bridge: the route declares which operation it represents, and the
    dependency arranges for ``current_call_context`` to be set before
    the route body (and the orchestrator beneath it) runs.

    This is then used for anything that requires the call context, such at usage
    tracking in :class:`~TrackingLLMAdapter`.

    Use inline at the route, e.g.
    ``Depends(call_scope_for(Operation.ANALYZE))``. FastAPI evaluates the
    default value once at module-load time (when the route function is
    defined), so there's no per-request cost to inlining.

    Parameters
    ----------
    operation : Operation
        The public orchestrator operation this dependency represents.

    Returns
    -------
    Callable[..., AsyncIterator[CallContext]]
        A FastAPI dependency suitable for ``Depends(...)``.
    """

    async def _scope(
        request: Request,
        tenant: TenantApiKey = Depends(authenticate_request),
    ) -> AsyncIterator[CallContext]:
        async with call_scope(
            tenant_id=tenant.tenant_id,
            operation=operation,
            request_id=UUID(request.state.request_id),
        ) as ctx:
            yield ctx

    return _scope


def require_superuser(
    tenant: TenantApiKey = Depends(authenticate_request),
) -> TenantApiKey:
    """FastAPI dependency that authenticates and checks superuser status.

    Parameters
    ----------
    tenant : TenantApiKey
        The authenticated tenant (injected by ``authenticate_request``).

    Returns
    -------
    TenantApiKey
        The authenticated superuser tenant.

    Raises
    ------
    AuthorizationError
        If the tenant is not a superuser.
    """
    if not tenant.is_superuser:
        raise AuthorizationError("Superuser access required")
    return tenant
