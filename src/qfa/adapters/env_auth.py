"""Environment-based authentication lookup adapter."""

from qfa.domain.models import AuthKeyInfo, TenantApiKey
from qfa.domain.ports import AuthLookupPort


class EnvironmentAuthLookupAdapter(AuthLookupPort):
    """AuthLookupPort implementation backed by a static list of API keys.

    Keys are injected at construction time (e.g. loaded from the
    ``AUTH_API_KEYS`` environment variable via ``AuthSettings``).  No
    external I/O is performed; every lookup is an in-process scan.

    Parameters
    ----------
    api_keys : list[TenantApiKey]
        The full set of valid API keys to validate against.
    """

    def __init__(self, api_keys: list[TenantApiKey]) -> None:
        self._api_keys = list(api_keys)

    async def validate_api_key(self, provided_key: str) -> TenantApiKey | None:
        """Return the matching TenantApiKey, or None if no key matches.

        Uses ``TenantApiKey.matches_key`` (``secrets.compare_digest``) for
        constant-time comparison and always iterates **all** keys to avoid
        leaking information about how many keys are registered.

        Parameters
        ----------
        provided_key : str
            The API key value supplied by the caller.

        Returns
        -------
        TenantApiKey | None
            The matching tenant API key, or ``None`` if no match was found.
        """
        match: TenantApiKey | None = None

        for api_key in self._api_keys:
            if api_key.matches_key(provided_key):
                match = api_key

        return match

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[AuthKeyInfo]:
        """Return API key metadata for the given tenant, or all tenants.

        Sensitive fields (``hashed_key``) are excluded from the returned
        dicts.

        Parameters
        ----------
        tenant_id : str | None
            Filter by this tenant identifier, or ``None`` to return keys
            for all tenants.

        Returns
        -------
        list[AuthKeyInfo]
            A list of AuthKeyInfo objects with auth key information (no secret values).
        """
        keys = (
            self._api_keys
            if tenant_id is None
            else [k for k in self._api_keys if k.tenant_id == tenant_id]
        )
        return [AuthKeyInfo(**k.model_dump(exclude={"hashed_key"})) for k in keys]
