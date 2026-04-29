"""LLM client adapter using LiteLLM for unified provider access."""

import logging
import re
from typing import cast

import openai
from litellm import acompletion, completion_cost
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine, OperatorConfig
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_exponential

from qfa.domain import AnalysisError, DocumentsTooLargeError
from qfa.domain.errors import LLMError, LLMRateLimitError, LLMTimeoutError
from qfa.domain.models import LLMResponse, T_Response
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
        self._analyzer: AnalyzerEngine = AnalyzerEngine()
        self._anonymizer: AnonymizerEngine = AnonymizerEngine()

    def _get_unique_id(
        self, original_value: str, entity_type: str, mapping: dict[str, str]
    ) -> str:
        """Helper to create unique IDs and store them in our map."""
        if original_value == "PII":
            return "<PII>"

        for placeholder, value in mapping.items():
            if value == original_value and placeholder.startswith(f"<{entity_type}_"):
                return placeholder

        placeholder = f"<{entity_type}_{len(mapping.keys())}>"
        mapping[placeholder] = original_value
        return placeholder

    def _anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        """Anonimize text with placeholders."""
        mapping: dict[str, str] = {}
        self.count = 0

        results = self._analyzer.analyze(text=text, language="en")
        unique_entities = {res.entity_type for res in results}

        # We use a custom lambda as the operator
        operators = {}
        for entity in unique_entities:
            operators[entity] = OperatorConfig(
                "custom",
                {
                    # Capture 'entity' as a default argument 'ent' to avoid closure issues
                    "lambda": lambda x, ent=entity: self._get_unique_id(x, ent, mapping)
                },
            )

        # Preserve DATE_TIME entities without anonymization
        operators["DATE_TIME"] = OperatorConfig("keep")

        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,  # type: ignore
            operators=operators,
        )
        return anonymized.text, mapping

    def _deanonymize(self, text: str, mapping: dict) -> str:
        """Restore original values in text by replacing anonymized placeholders."""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text

    def _check_injection(self, user_message: str) -> None:
        """Scan user_message for known prompt injection strings.

        Parameters
        ----------
        user_message : str
            The prompt.

        Raises
        ------
        AnalysisError
            When a document matches an injection pattern.
        """
        _INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
            (
                "role_prefix",
                re.compile(r"^\s*(SYSTEM|ASSISTANT|USER)\s*:", re.IGNORECASE),
            ),
            ("null_byte", re.compile(r"\x00")),
            ("repeated_chars", re.compile(r"(.)\1{199,}")),
        ]

        for pattern_name, pattern in _INJECTION_PATTERNS:
            if pattern.search(user_message):
                logger.warning(
                    "Prompt injection detected: pattern=%s",
                    pattern_name,
                )
                msg = f"Prompt injection detected pattern={pattern_name}"
                raise AnalysisError(msg)

    def _check_token_limit(self, system_message: str, user_message: str) -> None:
        """Estimate total tokens and raise if over the limit.

        Parameters
        ----------
        system_message : str
            The assembled system message.
        user_message : str
            The assembled user message (documents block).

        Raises
        ------
        DocumentsTooLargeError
            When estimated tokens exceed the configured limit.
        """
        CHARS_PER_TOKEN = 4  # TODO use LLMSettings
        MAX_TOTAL_TOKENS = 100_000  # TODO use LLMSettings

        assembled_text = system_message + user_message
        estimated_tokens = len(assembled_text) // CHARS_PER_TOKEN
        if estimated_tokens > MAX_TOTAL_TOKENS:
            msg = (
                f"Estimated tokens ({estimated_tokens}) exceed limit "
                f"({MAX_TOTAL_TOKENS})"
            )
            raise DocumentsTooLargeError(
                msg,
                estimated_tokens=estimated_tokens,
                limit=MAX_TOTAL_TOKENS,
            )

    @retry(
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_delay(60),
        retry=retry_if_exception_type((LLMTimeoutError, LLMRateLimitError)),
    )
    async def complete(
        self,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
        anonymize: bool = True,
        timeout: float = 20.0,
    ) -> LLMResponse[T_Response]:
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
        self._check_injection(user_message)

        self._check_token_limit(system_message, user_message)

        if anonymize:
            user_message, anonymization_mapping = self._anonymize(user_message)

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
                response_format=response_model
                if issubclass(response_model, BaseModel)
                else None,
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

        usage = response.usage
        if usage is None:
            raise LLMError("LLM response missing usage data")

        try:
            cost = completion_cost(completion_response=response)
        except Exception:
            logger.error("No pricing data for model %s", self._model)
            cost = float("nan")

        if anonymize:
            content = self._deanonymize(content, anonymization_mapping)

        if issubclass(response_model, BaseModel):
            parsed_data: T_Response = cast(
                T_Response, response_model.model_validate_json(content)
            )
        else:
            parsed_data = cast(T_Response, content)

        return LLMResponse[T_Response](
            structured=parsed_data,
            model=response.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost=cost,
        )
