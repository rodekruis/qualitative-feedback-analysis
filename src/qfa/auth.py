"""Authentication utilities for API key loading and validation."""

import json
import pathlib
import secrets

from pydantic import ValidationError

from qfa.domain.errors import AuthenticationError
from qfa.domain.models import TenantApiKey


def load_api_keys(config_path: pathlib.Path) -> list[TenantApiKey]:
    """Load API keys from a JSON config file.

    Expected JSON structure::

        [
            {
                "name": "crm-production",
                "key": "sk-prod-abc123...",
                "tenant_id": "tenant-redcross-nl"
            }
        ]

    Parameters
    ----------
    config_path : pathlib.Path
        Path to the JSON configuration file containing API keys.

    Returns
    -------
    list[TenantApiKey]
        Parsed list of tenant API keys.

    Raises
    ------
    FileNotFoundError
        If *config_path* does not exist.
    ValueError
        If the JSON is malformed or entries are invalid.
    """
    if not config_path.exists():
        msg = f"API key config file not found: {config_path}"
        raise FileNotFoundError(msg)

    text = config_path.read_text(encoding="utf-8")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON in API key config: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(raw, list):
        msg = "API key config must be a JSON array"
        raise ValueError(msg)

    keys: list[TenantApiKey] = []
    for idx, entry in enumerate(raw):
        try:
            keys.append(TenantApiKey(**entry))
        except (TypeError, ValidationError) as exc:
            msg = f"Invalid API key entry at index {idx}: {exc}"
            raise ValueError(msg) from exc

    return keys


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
