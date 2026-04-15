"""LLM client adapter for OpenAI and Azure OpenAI providers."""

import logging

import openai
from openai import AsyncAzureOpenAI, AsyncOpenAI

from qfa.domain.errors import (
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import LLMResponse
from qfa.domain.ports import LLMPort

logger = logging.getLogger(__name__)


class OpenAiLLMClient(LLMPort):
    """LLM adapter satisfying LLMPort.

    Wraps ``AsyncOpenAI`` or ``AsyncAzureOpenAI`` to translate OpenAI SDK
    Responses API calls into the domain ``LLMResponse`` model. Exception
    mapping converts SDK-specific errors into domain errors so that upper
    layers remain provider-agnostic.

    Parameters
    ----------
    client : AsyncOpenAI | AsyncAzureOpenAI
        A pre-configured async OpenAI client instance.
    model : str
        The model identifier to use for responses (e.g. ``"gpt-4"``).
    """

    def __init__(self, client: AsyncOpenAI | AsyncAzureOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ) -> LLMResponse:
        """Send a completion request to the OpenAI Responses API.

        Parameters
        ----------
        system_message : str
            The system-level instruction for the model.
        user_message : str
            The user-level message to complete.
        timeout : float
            Maximum time in seconds to wait for a response.
        tenant_id : str
            Tenant identifier passed as ``user`` for audit trail.

        Returns
        -------
        LLMResponse
            The model's response including token usage.

        Raises
        ------
        LLMTimeoutError
            When the OpenAI API does not respond in time.
        LLMRateLimitError
            When the OpenAI API returns a rate-limit response.
        LLMError
            For any other OpenAI API error.
        """
        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=system_message,
                input=user_message,
                store=False,
                user=tenant_id,
                timeout=timeout,
            )
        except openai.APITimeoutError as exc:
            logger.error(exc)
            raise LLMTimeoutError(str(exc)) from exc
        except openai.RateLimitError as exc:
            logger.error(exc)
            raise LLMRateLimitError(str(exc)) from exc
        except openai.APIError as exc:
            logger.error(exc)
            raise LLMError(str(exc)) from exc

        content = response.output_text
        if not content:
            raise LLMError("LLM returned empty content")

        usage = response.usage
        if usage is None:
            raise LLMError("LLM response missing usage data")

        return LLMResponse(
            text=content,
            model=response.model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
