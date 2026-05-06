"""Port interfaces (protocols) for the feedback analysis backend.

Driven ports declared here use ``typing.Protocol`` for structural
subtyping per ADR-002. The orchestrator is exposed as the concrete
``Orchestrator`` class per ADR-011 (no driving port).
"""

from typing import Protocol

from qfa.domain.models import LLMResponse, T_Response


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
