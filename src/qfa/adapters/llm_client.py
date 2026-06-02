"""LLM client adapter using LiteLLM for unified provider access."""

import logging
import re
from typing import cast

from litellm import acompletion, completion_cost
from litellm.exceptions import APIError, BadRequestError, RateLimitError, Timeout
from litellm.utils import type_to_response_format_param
from pydantic import BaseModel, ValidationError
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_delay,
    wait_exponential,
)

from qfa.domain import AnalysisError, FeedbackTooLargeError
from qfa.domain.errors import (
    LLMBadRequestError,
    LLMContentPolicyViolationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import LLMResponse, T_Response
from qfa.domain.ports import LLMPort
from qfa.utils import timed

logger = logging.getLogger(__name__)

# JSON-Schema validation keywords that some structured-output providers reject
# in a ``response_format`` schema — Azure AI Mistral, for one, answers a schema
# carrying ``minimum`` with "Received unsupported keyword `minimum` in schema".
# They are exactly what Pydantic ``Field`` constraints serialise to (ge/le/gt/lt
# -> minimum/maximum/exclusive*, min_length/max_length -> minLength/maxLength,
# pattern, ...). The schema we send the model is only a generation hint — the
# authoritative validation is ``model_validate_json`` on the response — so
# stripping these from the *outgoing* schema costs no safety, and lets the
# domain models keep their constraints (and the OpenAPI docs they produce).
_UNSUPPORTED_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


def _strip_unsupported_schema_keywords(node: object) -> object:
    """Return ``node`` with unsupported validation keywords removed, recursively.

    Produces a new structure (the input is not mutated) and walks nested
    objects, ``$defs`` and array ``items`` so constraints on nested models are
    stripped too.
    """
    if isinstance(node, dict):
        return {
            key: _strip_unsupported_schema_keywords(value)
            for key, value in node.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYWORDS
        }
    if isinstance(node, list):
        return [_strip_unsupported_schema_keywords(item) for item in node]
    return node


def _provider_safe_response_format(model: type[BaseModel]) -> dict:
    """Build a ``response_format`` for ``model`` that any provider can ingest.

    Uses LiteLLM's own Pydantic->response_format conversion so the structure
    matches what already works across providers, then strips the validation
    keywords some providers reject from the schema it carries.
    """
    response_format = type_to_response_format_param(response_format=model)
    return cast(dict, _strip_unsupported_schema_keywords(response_format))


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
        self,
        model: str,
        api_key: str,
        api_base: str,
        api_version: str,
        chars_per_token: int,
        max_total_tokens: int,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._api_version = api_version
        self._chars_per_token = chars_per_token
        self._max_total_tokens = max_total_tokens

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
            The assembled user message containing the feedback records.

        Raises
        ------
        FeedbackTooLargeError
            When estimated tokens exceed the configured limit.
        """
        assembled_text = system_message + user_message
        estimated_tokens = len(assembled_text) // self._chars_per_token
        if estimated_tokens > self._max_total_tokens:
            msg = (
                f"Estimated tokens ({estimated_tokens}) exceed limit "
                f"({self._max_total_tokens})"
            )
            raise FeedbackTooLargeError(
                msg,
                estimated_tokens=estimated_tokens,
                limit=self._max_total_tokens,
            )

    @retry(
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_delay(60),
        retry=retry_if_exception_type((LLMTimeoutError, LLMRateLimitError)),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
        after=after_log(logger, logging.DEBUG),
    )
    async def complete(
        self,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
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

        logger.debug(
            "LiteLLMClient: dispatching message with timeout %.1f seconds", timeout
        )
        with timed() as call_sw:
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
                    response_format=_provider_safe_response_format(response_model)
                    if issubclass(response_model, BaseModel)
                    else None,
                )
            except Timeout as exc:
                # TODO retry
                logger.error(exc)
                raise LLMTimeoutError(str(exc)) from exc
            except RateLimitError as exc:
                # TODO back off and retry downstream
                logger.error(exc)
                raise LLMRateLimitError(str(exc)) from exc
            except BadRequestError as exc:
                logger.error(exc)
                msg = str(exc)
                if "filtered" in msg and "content management policy" in msg:
                    raise LLMContentPolicyViolationError(str(exc)) from exc
                else:
                    raise LLMBadRequestError(str(exc)) from exc
            except APIError as exc:
                logger.error(exc)
                raise LLMError(str(exc)) from exc

        content = response.choices[0].message.content
        if content is None:
            raise LLMError("LLM response missing content")
        if not isinstance(content, str):
            msg = f"LLM response content must be a string, got {type(content).__name__}"
            raise LLMError(msg)

        usage = response.usage
        if usage is None:
            raise LLMError("LLM response missing usage data")

        try:
            cost = completion_cost(completion_response=response)
        except Exception:
            logger.error("No pricing data for model %s", self._model)
            cost = float("nan")

        if issubclass(response_model, BaseModel):
            try:
                parsed_data: T_Response = cast(
                    T_Response, response_model.model_validate_json(content)
                )
            except ValidationError as exc:
                raise LLMError(
                    f"LLM response validation failed for {response_model.__name__}: {exc}"
                ) from exc
        elif issubclass(response_model, str):
            parsed_data = content
        else:
            raise ValueError(
                "The `response_model` is not a string or BaseModel subclass."
            )

        # Per-call latency + usage. All fields here are explicitly safe to log
        # (see docs/operations/observability.md) — no message text, prompt, or
        # response content. DEBUG because hierarchical analysis fans out one of
        # these per chunk plus judges and reduces; INFO would be very chatty.
        logger.debug(
            "LLM call: model=%s latency=%.2fs prompt_tokens=%d "
            "completion_tokens=%d cost=%s",
            response.model,
            call_sw.elapsed_seconds,
            usage.prompt_tokens,
            usage.completion_tokens,
            cost,
        )

        return LLMResponse[T_Response](
            structured=parsed_data,
            model=response.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost=cost,
        )
