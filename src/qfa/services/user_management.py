"""Application service for user authentication and user management operations."""

from pydantic import SecretStr

from qfa.domain import AuthenticationError, TenantApiKey
from src.qfa.domain.ports import AuthLookupPort, UserManagementPort


class UserManager:
    """Coordinate user lookup and user management across configured ports."""

    def __init__(
        self,
        auth_lookup_ports: list[AuthLookupPort],
        user_management_ports: list[UserManagementPort],
    ):
        """Initialize a user manager with lookup and management ports.

        Parameters
        ----------
        auth_lookup_ports : list[AuthLookupPort]
            One or more authentication lookup ports used to validate keys
            and list users.
        user_management_ports : list[UserManagementPort]
            Exactly one user-management port used for mutations.

        Raises
        ------
        ValueError
            If no auth lookup ports are provided, or if the number of
            user-management ports is not exactly one.
        """
        if len(auth_lookup_ports) == 0:
            raise ValueError(
                "UserManager should be instantiated with at least one auth_lookup_port."
            )
        if len(user_management_ports) != 1:
            raise ValueError(
                "UserManager should be instantiated with exactly one user_management_port. "
                f"Given: {len(user_management_ports)}"
            )

        self.auth_lookup_ports = auth_lookup_ports
        self.user_management_ports = user_management_ports

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

        raise AuthenticationError("API key does not match any known user.")

    def get_all_users(self) -> list[TenantApiKey]:
        """Return the combined user list from all authentication backends.

        Returns
        -------
        list[TenantApiKey]
            All tenant API key records exposed by configured lookup ports.
        """
        all_users: list[TenantApiKey] = []
        for auth_lookup_port in self.auth_lookup_ports:
            all_users.extend(auth_lookup_port.get_all_users())
        return all_users

    def add_user(self, tenant_api_key: TenantApiKey) -> None:
        """Add a user through the configured user-management backend.

        Parameters
        ----------
        tenant_api_key : TenantApiKey
            The tenant API key record to add.
        """
        self.user_management_ports[0].add_user(tenant_api_key)

    def delete_user(self, key_id: str) -> None:
        """Delete a user through the configured user-management backend.

        Parameters
        ----------
        key_id : str
            Unique identifier of the tenant API key record to remove.
        """
        self.user_management_ports[0].delete_user(key_id)
