"""LLM client adapter using LiteLLM for unified provider access."""

import logging

import logging

import openai
from litellm import acompletion, completion_cost

from qfa.domain.errors import LLMError, LLMRateLimitError, LLMTimeoutError
from qfa.domain.models import LLMResponse
from qfa.domain.ports import LLMPort

logger = logging.getLogger(__name__)



class LiteLLMClient(LLMPort):
    """LLM adapter satisfying LLMPort via LiteLLM.

    Routes to any LLM provider based on the model string prefix
    (e.g. ``"azure/gpt-4"``, ``"azure_ai/mistral-large-2411"``).
    Calculates per-call cost using LiteLLM's built-in cost map
    or custom pricing registered via ``litellm.register_model()``.

    Parameters
    ----------
    model : str
        LiteLLM model identifier (e.g. ``"azure_ai/mistral-large-2411"``).
    api_key : str
        API key for the provider.
    api_base : str
        Base URL for the provider endpoint. Empty string if not needed.
    api_version : str
        API version string. Empty string if not needed.
    """

    def __init__(
        self, model: str, api_key: str, api_base: str, api_version: str
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._api_version = api_version

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ) -> LLMResponse:
        """Send a completion request via LiteLLM.

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
            The model's response including token usage and cost.

        Raises
        ------
        LLMTimeoutError
            When the provider does not respond in time.
        LLMRateLimitError
            When the provider returns a rate-limit response.
        LLMError
            For any other provider error or empty response.
        """
        try:
            response = await acompletion(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                api_key=self._api_key,
                api_base=self._api_base or None,
                api_version=self._api_version or None,
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

        content = response.choices[0].message.content
        if not content:
            raise LLMError("LLM returned empty content")

        usage = response.usage
        if usage is None:
            raise LLMError("LLM response missing usage data")

        try:
            cost = completion_cost(completion_response=response)
        except Exception:
            logger.warning("No pricing data for model %s", self._model)
            cost = None

        return LLMResponse(
            text=content,
            model=response.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost=cost,
        )
