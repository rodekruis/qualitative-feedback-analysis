"""FastAPI dependency functions for authentication and service injection."""

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qfa.domain.errors import AuthenticationError, AuthorizationError
from qfa.domain.models import TenantApiKey
from qfa.domain.ports import UsageRepositoryPort
from qfa.services.auth_orchestrator import AuthOrchestrator
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
