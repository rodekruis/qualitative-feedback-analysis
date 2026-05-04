"""Port interfaces (protocols) for the feedback analysis backend.

Driven ports declared here use ``typing.Protocol`` for structural
subtyping per ADR-002. The orchestrator is exposed as the concrete
``Orchestrator`` class per ADR-011 (no driving port).
"""

import datetime as dt
from typing import Protocol

from qfa.domain.models import (
    LLMCallRecord,
    LLMResponse,
    T_Response,
    UsageStats,
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

    async def get_usage_stats(
        self,
        tenant_id: str,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
    ) -> UsageStats | None:
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
        UsageStats | None
            Stats for the tenant, or None if no calls in window.
        """
        ...

    async def get_all_usage_stats(
        self,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
    ) -> list[UsageStats]:
        """Get per-tenant stats plus a grand total entry (tenant_id=None).

        Parameters
        ----------
        from_ : datetime | None
            Inclusive lower bound (UTC tz-aware), or None.
        to : datetime | None
            Exclusive upper bound (UTC tz-aware), or None.

        Returns
        -------
        list[UsageStats]
            Per-tenant stats followed by a grand total entry.
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
