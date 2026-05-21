"""Port interfaces (protocols) for the feedback analysis backend.

Driven ports declared here use ``typing.Protocol`` for structural
subtyping per ADR-002. The orchestrator is exposed as the concrete
``Orchestrator`` class per ADR-011 (no driving port).
"""

import datetime as dt
from typing import Protocol

from qfa.domain.models import (
    AuthKeyInfo,
    KeyCreationResponse,
    LLMResponse,
    T_Response,
    TenantApiKey,
    TenantInfo,
)
from qfa.domain.usage_models import (
    LLMCallRecord,
    OperationUsageStats,
    TenantUsageStats,
)


class LLMPort(Protocol):
    """Port for interacting with a large-language-model provider.

    Implementations must translate provider-specific details into the
    domain ``LLMResponse`` model.
    """

    async def complete(
        self,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
        timeout: float = 20.0,
    ) -> LLMResponse[T_Response]:
        """Send a completion request to the LLM provider.

        Parameters
        ----------
        system_message : str
            The system-level instruction for the model.
        user_message : str
            The user-level message to complete.
        tenant_id : str
            Tenant identifier for tracking and billing.
        response_model : type[T_Response]
            The Pydantic model to parse the response into.
        timeout : float
            Maximum time in seconds to wait for a response.

        Returns
        -------
        LLMResponse
            The model's response including token usage.
        """
        ...


class UsageRepositoryPort(Protocol):
    """Port for recording and querying LLM usage data."""

    async def record_call(self, record: LLMCallRecord) -> None:
        """Record a single LLM call attempt.

        Parameters
        ----------
        record : LLMCallRecord
            The call record to persist.
        """
        ...

    async def get_usage_stats_for_one_tenant(
        self,
        tenant_id: str,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
    ) -> TenantUsageStats:
        """Get aggregated usage stats for a single tenant.

        Parameters
        ----------
        tenant_id : str
            The tenant to query.
        from_ : datetime | None
            Inclusive lower bound (UTC tz-aware), or None.
        to : datetime | None
            Exclusive upper bound (UTC tz-aware), or None.

        Returns
        -------
        TenantUsageStats | None
            Stats for the tenant, or None if no calls in window.
        """
        ...

    async def get_all_usage_by_tenant(
        self,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
    ) -> list[TenantUsageStats]:
        """Get per-tenant stats plus a grand total entry (tenant_id=None).

        Parameters
        ----------
        from_ : datetime | None
            Inclusive lower bound (UTC tz-aware), or None.
        to : datetime | None
            Exclusive upper bound (UTC tz-aware), or None.

        Returns
        -------
        list[TenantUsageStats]
            Per-tenant stats followed by a grand total entry.
        """
        ...

    async def get_all_usage_by_operation(
        self,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
    ) -> list[OperationUsageStats]:
        """Get per-operation stats with nested per-tenant breakdown plus grand total.

        Inverse hierarchy of :meth:`get_all_usage_by_tenant`: top-level
        aggregation is by orchestrator operation; each operation block
        carries a tuple of per-tenant blocks. The grand-total entry
        (``operation=None``) is always emitted last, matching the
        convention used by :meth:`get_all_usage_by_tenant`.

        Parameters
        ----------
        from_ : datetime | None
            Inclusive lower bound (UTC tz-aware), or None.
        to : datetime | None
            Exclusive upper bound (UTC tz-aware), or None.

        Returns
        -------
        list[OperationUsageStats]
            Per-operation stats followed by a grand total entry.
        """
        ...


class AnonymizationPort(Protocol):
    """Port for anonymising and de-anonymising user-supplied text.

    Implementations replace named entities (people, locations, phone
    numbers, etc.) in ``text`` with stable placeholders, returning the
    redacted text together with a mapping that can be used to restore
    the original values via ``deanonymize``.

    Implementations must be deterministic for a given input within a
    single call (same entity replaced by the same placeholder).
    """

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace sensitive entities in ``text`` with placeholders.

        Parameters
        ----------
        text : str
            The text to anonymise.

        Returns
        -------
        tuple[str, dict[str, str]]
            The anonymised text and a mapping from placeholder to
            original value, suitable for passing to ``deanonymize``.
        """
        ...

    def deanonymize(self, text: str, mapping: dict[str, str]) -> str:
        """Restore original values in ``text`` using ``mapping``.

        Parameters
        ----------
        text : str
            The anonymised text, possibly containing placeholders.
        mapping : dict[str, str]
            Placeholder-to-original mapping returned by ``anonymize``.

        Returns
        -------
        str
            The text with placeholders replaced by original values.
        """
        ...


class AuthLookupPort(Protocol):
    """Port for authenticating users of the application."""

    async def validate_api_key(self, provided_key: str) -> TenantApiKey | None:
        """Validate if a key exists in the implemented adapter.

        Parameters
        ----------
        provided_key : str
            The API key value supplied by the caller.

        Returns
        -------
        TenantApiKey | None
            The matching tenant API key, or None if no match was found.
        """
        ...

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[AuthKeyInfo]:
        """Get all API keys for a tenant, or all keys if tenant_id is None.

        Parameters
        ----------
        tenant_id : str | None
            The tenant to query, or None to get keys for all tenants.

        Returns
        -------
        list[AuthKeyMetadata]
            A list of AuthKeyMetadata objects
        """
        ...


class AuthManagementPort(Protocol):
    """Port for adding/ removing keys and tenants from the application."""

    async def add_tenant(
        self, tenant_name: str, allows_superusers: bool = False
    ) -> str:
        """Add a new tenant to the implemented adapter and return its unique identifier.

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
        ...

    async def delete_tenant(self, tenant_id: str) -> None:
        """Delete an existing tenant from the implemented adapter.

        Parameters
        ----------
        tenant_id : str
            The unique identifier of the tenant to delete.

        Raises
        ------
        TenantNotFoundError:
            If no tenant with this tenant_id exists
        """
        ...

    async def add_key(
        self,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> KeyCreationResponse:
        """Generate and persist a new API key in the implemented adapter.

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
            The plaintext key is returned only once at creation time.

        Raises
        ------
        KeyAlreadyExistsError:
            If key with this key_id already exists
        TenantDoesNotAllowSuperUsersError:
            If the tenant does not allow superuser keys and is_superuser is True
        """
        ...

    async def delete_key(self, key_id: str) -> None:
        """Delete an existing API key from the implemented adapter.

        Parameters
        ----------
        key_id : str
            The unique identifier of the API key record to remove.
        """
        ...

    async def get_tenants(self) -> list[TenantInfo]:
        """Return metadata for all tenants in the implemented adapter.

        Returns
        -------
        list[TenantInfo]
            A list of TenantInfo objects with tenant metadata.
        """
        ...
