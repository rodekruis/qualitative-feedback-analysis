"""Authentication utilities for API key validation."""

import secrets

from qfa.domain.errors import AuthenticationError
from qfa.domain.models import TenantApiKey


def validate_api_key(
    provided_key: str,
    api_keys: list[TenantApiKey],
) -> TenantApiKey:
    """Validate a provided API key against the loaded keys.

    Uses ``secrets.compare_digest`` for constant-time comparison.
    Compares against **all** keys to avoid timing attacks.

    Parameters
    ----------
    provided_key : str
        The API key value supplied by the caller.
    api_keys : list[TenantApiKey]
        The loaded set of valid API keys.

    Returns
    -------
    TenantApiKey
        The matching tenant API key.

    Raises
    ------
    AuthenticationError
        If no loaded key matches *provided_key*.
    """
    match: TenantApiKey | None = None

    for api_key in api_keys:
        if secrets.compare_digest(provided_key, api_key.key):
            match = api_key

    if match is None:
        raise AuthenticationError("Invalid API key")

    return match
