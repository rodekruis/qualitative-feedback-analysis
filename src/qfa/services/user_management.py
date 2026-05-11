"""Application service for authentication and management operations."""

from pydantic import SecretStr

from qfa.domain import AuthenticationError, TenantApiKey
from src.qfa.domain.ports import AuthLookupPort, AuthManagementPort


class AuthOrchestrator:
    """Coordinate key and tenant lookup and management across configured ports."""

    def __init__(
        self,
        auth_lookup_ports: list[AuthLookupPort],
        auth_management_ports: list[AuthManagementPort],
    ):
        """Initialize a AuthOrchestrator manager with lookup and management ports.

        Parameters
        ----------
        auth_lookup_ports : list[AuthLookupPort]
            One or more authentication lookup ports used to validate keys
            and list keys.
        key_management_ports : list[AuthManagementPort]
            Exactly one auth-management port used for mutations.

        Raises
        ------
        ValueError
            If no auth lookup ports are provided, or if the number of
            auth-management ports is not exactly one.
        """
        if len(auth_lookup_ports) == 0:
            raise ValueError(
                "AuthOrchestrator should be instantiated with at least one auth_lookup_port."
            )
        if len(auth_management_ports) != 1:
            raise ValueError(
                "AuthOrchestrator should be instantiated with exactly one auth_management_port. "
                f"Given: {len(auth_management_ports)}"
            )

        self.auth_lookup_ports = auth_lookup_ports
        self.auth_management_ports = auth_management_ports

    def validate_api_key(self, api_key: SecretStr) -> TenantApiKey:
        """Validate an API key against available authentication backends.

        Parameters
        ----------
        api_key : SecretStr
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
            tenant_api_key = auth_lookup_port.validate_api_key(
                api_key.get_secret_value()
            )
            if tenant_api_key is None:
                continue
            return tenant_api_key

        raise AuthenticationError("API key does not match any known key.")

    def get_all_keys(self) -> list[TenantApiKey]:
        """Return the combined key list from all authentication backends.

        Returns
        -------
        list[TenantApiKey]
            All tenant API key records exposed by configured lookup ports.
        """
        all_keys: list[TenantApiKey] = []
        for auth_lookup_port in self.auth_lookup_ports:
            all_keys.extend(auth_lookup_port.get_all_keys())
        return all_keys

    def add_key(self, tenant_api_key: TenantApiKey) -> None:
        """Add a key through the configured auth-management backend.

        Parameters
        ----------
        tenant_api_key : TenantApiKey
            The tenant API key record to add.
        """
        self.auth_management_ports[0].add_key(tenant_api_key)

    def delete_key(self, key_id: str) -> None:
        """Delete a kay through the configured auth-management backend.

        Parameters
        ----------
        key_id : str
            Unique identifier of the tenant API key record to remove.
        """
        self.auth_management_ports[0].delete_key(key_id)
