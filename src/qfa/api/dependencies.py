"""FastAPI dependency functions for authentication and service injection."""

from fastapi import Request, Security
from fastapi.exceptions import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qfa.auth import validate_api_key
from qfa.domain.errors import AuthenticationError, AuthorizationError
from qfa.domain.models import TenantApiKey
from qfa.domain.ports import OrchestratorPort, UsageRepositoryPort


def get_orchestrator(request: Request) -> OrchestratorPort:
    """Return the orchestrator from app state.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.

    Returns
    -------
    OrchestratorPort
        The orchestrator service instance.
    """
    return request.app.state.orchestrator


def get_usage_repo(request: Request) -> UsageRepositoryPort:
    """Return the usage repository from app state, or raise 503 if disabled.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.

    Returns
    -------
    UsageRepositoryPort
        The usage repository instance.

    Raises
    ------
    HTTPException
        503 if usage tracking is not enabled.
    """
    repo = getattr(request.app.state, "usage_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail="Usage tracking is not enabled",
        )
    return repo


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
        return validate_api_key(credentials.credentials, request.app.state.api_keys)
    except AuthenticationError:
        raise AuthenticationError(error_message)


def require_superuser(
    tenant: TenantApiKey,
) -> TenantApiKey:
    """Raise AuthorizationError if the tenant is not a superuser.

    Parameters
    ----------
    tenant : TenantApiKey
        The authenticated tenant.

    Returns
    -------
    TenantApiKey
        The authenticated tenant (unchanged).

    Raises
    ------
    AuthorizationError
        If the tenant is not a superuser.
    """
    if not tenant.is_superuser:
        raise AuthorizationError("Superuser access required")
    return tenant
