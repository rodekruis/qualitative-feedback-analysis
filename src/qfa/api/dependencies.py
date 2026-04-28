"""FastAPI dependency functions for authentication and service injection."""

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qfa.auth import validate_api_key
from qfa.domain.errors import AuthenticationError
from qfa.domain.models import TenantApiKey
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
