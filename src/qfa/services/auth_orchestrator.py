"""Application service for authentication and management operations."""

from qfa.domain import AuthenticationError, TenantApiKey
from qfa.domain.ports import AuthLookupPort, AuthManagementPort


class AuthOrchestrator:
    """Coordinate key and tenant lookup and management across configured ports."""

    def __init__(
        self,
        auth_lookup_ports: list[AuthLookupPort],
        auth_management_port: AuthManagementPort,
    ):
        """Initialize a AuthOrchestrator manager with lookup and management ports.

        Parameters
        ----------
        auth_lookup_ports : list[AuthLookupPort]
            One or more authentication lookup ports used to validate keys
            and list keys.
        auth_management_port : AuthManagementPort
            The auth-management port used for mutations.
        """
        if len(auth_lookup_ports) == 0:
            raise ValueError(
                "AuthOrchestrator should be instantiated with at least one auth_lookup_port."
            )

        self.auth_lookup_ports = auth_lookup_ports
        self.auth_management_port = auth_management_port

    async def validate_api_key(self, api_key: str) -> TenantApiKey:
        """Validate an API key against available authentication backends.

        Parameters
        ----------
        api_key : str
            API key supplied by the caller.

        Returns
        -------
        TenantApiKey
            The matching tenant API key record.

        Raises
        ------
        AuthenticationError
            If no configured authentication backend recognizes the key.
        """
        for auth_lookup_port in self.auth_lookup_ports:
            tenant_api_key = await auth_lookup_port.validate_api_key(api_key)
            if tenant_api_key is None:
                continue
            return tenant_api_key

        raise AuthenticationError("API key does not match any known key.")

    async def add_tenant(
        self, tenant_name: str, allows_superusers: bool = False
    ) -> str:
        """Add a new tenant through the configured auth-management backend.

        Parameters
        ----------
        tenant_name : str
            The name of the tenant to create.
        allows_superusers : bool
            Whether this tenant allows creation of superuser keys (default False).

        Returns
        -------
        str
            The unique identifier of the created tenant.
        """
        return await self.auth_management_port.add_tenant(
            tenant_name, allows_superusers
        )

    async def delete_tenant(self, tenant_id: str) -> None:
        """Delete an existing tenant through the configured auth-management backend.

        Parameters
        ----------
        tenant_id : str
            The unique identifier of the tenant to delete.
        """
        await self.auth_management_port.delete_tenant(tenant_id)

    async def add_key(
        self,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> tuple[str, str]:
        """Add a key through the configured auth-management backend.

        Parameters
        ----------
        key_name : str
            A human-friendly name for the key.
        tenant_id : str
            The tenant this key belongs to.
        is_superuser : bool
            Whether this key should have superuser privileges (default False).

        Returns
        -------
        tuple[str, str]
            The created key identifier and plaintext API key as ``(key_id, api_key)``.
        """
        return await self.auth_management_port.add_key(
            key_name,
            tenant_id,
            is_superuser,
        )

    async def delete_key(self, key_id: str) -> None:
        """Delete a key through the configured auth-management backend.

        Parameters
        ----------
        key_id : str
            Unique identifier of the tenant API key record to remove.
        """
        await self.auth_management_port.delete_key(key_id)

    async def get_tenants(self) -> list[dict]:
        """Get all tenants through the configured auth-management backend.

        Returns
        -------
        list[dict]
            List of tenant metadata dicts (tenant_id, name, allows_superusers).
        """
        return await self.auth_management_port.get_tenants()

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[dict]:
        """Get all API keys through the configured auth-management backend.

        Can be filtered by tenant_id if provided.

        Parameters
        ----------
        tenant_id : str
            Unique identifier of the tenant to list keys for.

        Returns
        -------
        list[dict]
            List of API key informations associated with the tenant.
        """
        auth_key_informations: list[dict] = []
        for auth_lookup_port in self.auth_lookup_ports:
            auth_key_informations.extend(
                await auth_lookup_port.get_auth_keys(tenant_id)
            )
        return auth_key_informations
