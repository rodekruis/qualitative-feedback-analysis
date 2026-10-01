"""LLM client adapter using LiteLLM for unified provider access."""

import json
import logging
import os
import re
from typing import cast

from litellm import acompletion, completion_cost
from litellm.exceptions import APIError, BadRequestError, RateLimitError, Timeout
from litellm.utils import type_to_response_format_param
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    after_log,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_delay,
    wait_exponential,
)

import qfa
from qfa.domain import FeedbackTooLargeError, PromptInjectionDetectedError
from qfa.domain.errors import (
    LLMBadRequestError,
    LLMContentPolicyViolationError,
    LLMError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMTimeoutError,
)
from qfa.domain.models import LLMResponse, T_Response
from qfa.domain.ports import LLMPort
from qfa.services.call_context import (
    current_call_context,
    current_call_role,
    otel_context_for,
)
from qfa.settings import LLM_RETRY_BUDGET_MULTIPLIER
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

# JSON-Schema token names a provider could plausibly name when it rejects a
# schema. Used only to *classify* a rejection for the diagnostic log line
# below — never to strip anything from an outgoing schema, which is what
# ``_UNSUPPORTED_SCHEMA_KEYWORDS`` above is for. Keeping the rejection
# classification inside a closed vocabulary is what lets us report *why* a
# provider refused without ever logging its text (ADR-018). Members that are
# also error-envelope keys (``type``, ``name``, ``description``, ``schema``)
# are safe here only because the match below is anchored to the rejection
# phrasing; an unanchored scan of the message would report them from the
# envelope the provider serialised around its own error.
_SCHEMA_KEYWORD_VOCABULARY: frozenset[str] = _UNSUPPORTED_SCHEMA_KEYWORDS | frozenset(
    {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "format",
        "default",
        "anyOf",
        "oneOf",
        "allOf",
        "$ref",
        "$defs",
        "strict",
        "name",
        "schema",
    }
)

# The rejection phrasing itself, quoted token included — recorded verbatim in
# 24165c8: "Received unsupported keyword `minimum` in schema". The literal
# prefix is load-bearing: litellm puts the provider's raw body in
# ``str(exc)``, and an OpenAI-style envelope quotes its own keys
# (``{"error": {"type": "invalid_request_error", ...}}``), so matching any
# quoted token would report ``type`` for every 400 — including ones whose
# message names the real culprit later in the body.
_REJECTED_KEYWORD_PATTERN = re.compile(
    r"""unsupported\s+keyword\s+\\?[`'"](\$?[A-Za-z][\w$]*)\\?[`'"]""",
    re.IGNORECASE,
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


def _provider_status(exc: Exception) -> int | None:
    """Return the provider's HTTP status code, or ``None`` if unavailable."""
    status_code = getattr(exc, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _retry_after_seconds(exc: Exception) -> int | None:
    """Return the provider's ``Retry-After`` header value in seconds.

    Reads the header only, never the exception text. Returns ``None`` when
    the header is missing, is an HTTP-date rather than an integer, or no
    response/headers are attached to ``exc`` at all.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        return int(headers["retry-after"])
    except (KeyError, TypeError, ValueError):
        return None


def _content_filter_signal(choice: object) -> tuple[str | None, str | None]:
    """Return the first flagged (category, severity) from a choice's content-filter annotation.

    Reads only the structured ``content_filter_results`` dict Azure attaches
    to a completion choice — category names and severity levels are a closed
    set, not free text (see ADR-018). LiteLLM's response converter only
    copies fields declared on its ``Choices`` model onto the choice itself;
    anything else the provider sent (``content_filter_results`` included)
    lands in ``choice.provider_specific_fields`` instead, so that is read
    here rather than a top-level attribute. Returns ``(None, None)`` when
    that field is absent, not a dict (e.g. a test double), or nothing in it
    was flagged.
    """
    provider_fields = getattr(choice, "provider_specific_fields", None)
    if not isinstance(provider_fields, dict):
        return None, None
    results = provider_fields.get("content_filter_results")
    if not isinstance(results, dict):
        return None, None
    for category, result in results.items():
        if isinstance(result, dict) and result.get("filtered"):
            return category, result.get("severity")
    return None, None


def _rejected_schema_keyword(message: str) -> str | None:
    """Return the schema token a provider named as unsupported, if any.

    Reads the provider string only to classify it, and returns a member of
    ``_SCHEMA_KEYWORD_VOCABULARY`` or nothing at all, so no provider-derived
    text is propagated (ADR-018) — the same read-but-never-repeat pattern as
    the content-filter sniff in :func:`_to_domain_error`. ``None`` when the
    message does not use the "unsupported keyword `x`" phrasing, or names a
    token outside the vocabulary. Deliberately narrow: ``message`` is the
    whole litellm exception string, provider error envelope and all, and a
    false positive here sends an operator to strip a keyword the provider
    never refused (see the runbook in ``docs/operations/observability.md``).
    """
    by_lowercase = {keyword.lower(): keyword for keyword in _SCHEMA_KEYWORD_VOCABULARY}
    for candidate in _REJECTED_KEYWORD_PATTERN.findall(message):
        keyword = by_lowercase.get(candidate.lower())
        if keyword is not None:
            return keyword
    return None


def _schema_key_names(node: object) -> list[str]:
    """Return every dict key occurring anywhere in ``node``, sorted and deduped.

    Walks the schema *this repo* built, so every name returned — JSON-Schema
    keywords and our own model field names alike — is repo-authored and safe
    to log (ADR-018).
    """
    keys: set[str] = set()
    pending: list[object] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            keys.update(str(key) for key in current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return sorted(keys)


def _to_domain_error(
    exc: Timeout | RateLimitError | BadRequestError | APIError,
    provider_status: int | None,
) -> LLMError:
    """Translate a litellm provider exception into a domain error with a fixed message.

    The message is always hand-written in this repo — never provider text
    (see ADR-018). The one exception is the Azure content-filter sniff
    below: it *reads* the provider string to choose between
    ``LLMContentPolicyViolationError`` and ``LLMBadRequestError``, but the
    string itself is never propagated into either error.
    """
    if isinstance(exc, Timeout):
        return LLMTimeoutError(
            "LLM provider timed out", provider_status=provider_status
        )
    if isinstance(exc, RateLimitError):
        return LLMRateLimitError(
            "LLM provider rate limit exceeded",
            provider_status=provider_status,
            retry_after=_retry_after_seconds(exc),
        )
    if isinstance(exc, BadRequestError):
        msg = str(exc)
        if "filtered" in msg and "content management policy" in msg:
            return LLMContentPolicyViolationError(
                "LLM provider rejected the request under its content policy",
                provider_status=provider_status,
            )
        return LLMBadRequestError(
            "LLM provider rejected the request", provider_status=provider_status
        )
    return LLMError("LLM provider call failed", provider_status=provider_status)


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
    tracer : Tracer | None
        Emits one span per :meth:`complete` call, for Langfuse call tracing
        (see :func:`qfa.api.composition.build_langfuse_tracer`). ``None``
        (the default) builds a tracer bound to a fresh, local
        ``TracerProvider`` with no span processor attached, so every span it
        creates is dropped and nothing is exported — deliberately *not* the
        process-global provider :mod:`qfa.telemetry` may install for
        Application Insights, which every caller of
        ``opentelemetry.trace.get_tracer`` shares.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: str,
        api_version: str,
        chars_per_token: int,
        max_total_tokens: int,
        tracer: Tracer | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._api_version = api_version
        self._chars_per_token = chars_per_token
        self._max_total_tokens = max_total_tokens
        self._tracer = (
            tracer if tracer is not None else TracerProvider().get_tracer(__name__)
        )

    def _check_injection(self, user_message: str) -> None:
        """Scan user_message for known prompt injection strings.

        Parameters
        ----------
        user_message : str
            The prompt.

        Raises
        ------
        PromptInjectionDetectedError
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
                raise PromptInjectionDetectedError(msg)

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

    async def _complete_once(
        self,
        *,
        system_message: str,
        user_message: str,
        tenant_id: str,
        timeout: float,
        response_format: dict | None,
    ):
        """Issue a single provider completion, translating provider errors.

        This is exactly one ``acompletion`` round-trip plus the boundary
        translation from litellm exceptions to ``qfa.domain.errors``. It does
        NOT retry, check the token limit, scan for injection, or parse the
        response — :meth:`complete` owns those (they must run once, not once
        per attempt). Factored out so the retry loop wraps only the network
        call, and so the error-mapping contract can be unit-tested on a single
        attempt without driving the retry loop.
        """
        try:
            return await acompletion(
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
                response_format=response_format,
            )
        except (Timeout, RateLimitError, BadRequestError, APIError) as exc:
            provider_status = _provider_status(exc)
            logger.error(
                "LLM provider error: type=%s status=%s model=%s",
                type(exc).__name__,
                provider_status,
                self._model,
            )
            if isinstance(exc, BadRequestError):
                json_schema = (response_format or {}).get("json_schema", {})
                # Names which schema of ours the provider refused and which
                # structured-output token it objected to. Every value is
                # either written here or drawn from a closed vocabulary —
                # none is provider text (ADR-018).
                logger.error(
                    "LLM provider rejected request: model=%s response_format=%s "
                    "schema_name=%s schema_keys=%s rejected_keyword=%s",
                    self._model,
                    response_format["type"] if response_format else "none",
                    json_schema.get("name", "none"),
                    ",".join(_schema_key_names(json_schema.get("schema"))),
                    _rejected_schema_keyword(str(exc)) or "unknown",
                )
            raise _to_domain_error(exc, provider_status) from exc

    async def complete(
        self,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
        timeout: float = 40.0,
    ) -> LLMResponse[T_Response]:
        """Send a completion request via LiteLLM, retrying transient failures.

        ``timeout`` is the budget for a *single* attempt. Transient failures
        (timeout, rate-limit) and content-policy rejections are retried with
        exponential backoff up to a total wall-clock budget of
        ``LLM_RETRY_BUDGET_MULTIPLIER * timeout``; the retry wraps only the
        provider call, so injection/token checks and response parsing happen
        exactly once. Callers that enforce a deadline must size ``timeout`` so
        this worst-case budget still fits (the orchestrator does this in
        ``_check_deadline_and_get_timeout``). Content-policy rejections are
        retried because Azure's filter severity classification is not
        guaranteed deterministic for identical input (#293); other bad-request
        and generic API errors are not retried — they are not transient.
        Azure signals a rejection two ways, both mapped to
        ``LLMContentPolicyViolationError`` and both retried: a synchronous
        ``BadRequestError`` (sniffed by ``_to_domain_error``), or a ``200``
        response whose ``choices[0].message.content`` is ``None`` with its
        ``content_filter_results`` flagging a category (Azure's asynchronous
        filter, which lets the call through and blocks the completion after
        generation). The asynchronous path bills a completion before
        rejecting it, so usage from every discarded attempt is accumulated
        and folded into whichever outcome this call ultimately produces: the
        returned ``LLMResponse``'s token/cost fields on eventual success, or
        the raised ``LLMContentPolicyViolationError``'s ``discarded_*``
        fields if every attempt is blocked.

        Also emits one Langfuse span (via ``self._tracer``) around the whole
        call, tagged with ``tenant_id`` and, inside an active
        ``current_call_context`` (unset for scripts/tests outside an HTTP
        request), the orchestrator ``operation`` plus the deployed
        ``version``/``commit`` as trace metadata. Also tagged (and named)
        as a judge call inside an active ``judge_call()`` block (see
        ``qfa.services.call_context``) — every current judge call site
        wraps itself in one. Opened with ``context=otel_context_for(ctx.call_id)``
        rather than the ambient OTel context, so every call made inside
        one ``call_scope`` shares one Langfuse trace id — a judge call's
        span lands in the same trace as the generation call it graded
        (#354) — without reparenting unrelated spans opened elsewhere
        during the request (see ``qfa.services.call_context``'s module
        docstring). Never carries the prompt, completion, or any
        other request/response content — only the same model/tokens/cost
        fields logged at DEBUG below and returned in ``LLMResponse``.

        Parameters
        ----------
        system_message : str
            The system-level instruction for the model.
        user_message : str
            The user-level message to complete.
        timeout : float
            Maximum time in seconds to wait for a single attempt.
        tenant_id : str
            Tenant identifier passed as ``user`` for audit trail.

        Returns
        -------
        LLMResponse
            The model's response including token usage and cost.

        Raises
        ------
        LLMTimeoutError
            When the provider does not respond in time on every attempt.
        LLMRateLimitError
            When the provider rate-limits on every attempt.
        LLMContentPolicyViolationError
            When the provider rejects the request under its content policy on
            every attempt.
        LLMBadRequestError
            When the provider rejects the request for any other reason.
        PromptInjectionDetectedError
            When the input matches a known prompt-injection pattern.
        LLMError
            For any other provider error or empty response.
        """
        ctx = current_call_context.get()
        is_judge = current_call_role.get() == "judge"
        # Langfuse's OTel ingestion treats one span as one observation of the
        # trace its trace id names, so this span's name is what both list
        # views show. Named by operation (when known) so the list is
        # scannable without opening a row or filtering by the
        # langfuse.trace.tags attribute below; "llm_call" is only the
        # fallback outside an HTTP request (scripts, notebooks, tests),
        # where no operation is known. Suffixed with ":judge" for a judge
        # call -- a generation call's name is unchanged, since that is the
        # common case. ``qfa.services.call_context.call_scope`` makes every
        # call inside one request share one trace id (#354), so a request
        # with both a generation and a judge call now shows as one trace
        # with two observations, not two separate traces.
        operation = ctx.operation if ctx is not None else "llm_call"
        span_name = f"{operation}:judge" if is_judge else operation
        parent_context = otel_context_for(ctx.call_id) if ctx is not None else None
        with self._tracer.start_as_current_span(
            span_name, context=parent_context
        ) as span:
            span.set_attribute("langfuse.observation.type", "generation")
            span.set_attribute("langfuse.user.id", tenant_id)
            if ctx is not None:
                tags = [ctx.operation, "judge"] if is_judge else [ctx.operation]
                span.set_attribute("langfuse.trace.tags", json.dumps(tags))
                span.set_attribute(
                    "langfuse.trace.metadata",
                    json.dumps(
                        {
                            "deployed_version": qfa.__version__,
                            "deployed_commit": os.environ.get("GIT_SHA", "unknown"),
                        }
                    ),
                )
            try:
                response = await self._complete_impl(
                    system_message, user_message, tenant_id, response_model, timeout
                )
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(type(exc).__name__)))
                raise
            span.set_attribute("langfuse.observation.model.name", response.model)
            span.set_attribute(
                "langfuse.observation.usage_details",
                json.dumps(
                    {
                        "input": response.prompt_tokens,
                        "output": response.completion_tokens,
                        "total": response.prompt_tokens + response.completion_tokens,
                    }
                ),
            )
            span.set_attribute(
                "langfuse.observation.cost_details",
                json.dumps({"total": response.cost}),
            )
            return response

    async def _complete_impl(
        self,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
        timeout: float = 40.0,
    ) -> LLMResponse[T_Response]:
        """Run the retry/parse logic :meth:`complete` wraps in a Langfuse span.

        Behaviour and contract are exactly :meth:`complete`'s docstring; split
        out only so the span in :meth:`complete` wraps a single call rather
        than being interleaved through this method's retry loop.
        """
        self._check_injection(user_message)

        self._check_token_limit(system_message, user_message)

        response_format = (
            _provider_safe_response_format(response_model)
            if issubclass(response_model, BaseModel)
            else None
        )

        retry_budget = LLM_RETRY_BUDGET_MULTIPLIER * timeout
        logger.debug(
            "LiteLLMClient: dispatching message with per-attempt timeout %.1fs "
            "(retry budget %.1fs)",
            timeout,
            retry_budget,
        )
        # Azure's asynchronous filter bills a completion before blocking it, so a
        # discarded attempt can still carry real provider spend. Accumulated here
        # and folded into whatever this call ultimately returns or raises, so a
        # caller recording usage never silently drops the cost of a retried,
        # filtered attempt.
        discarded_prompt_tokens = 0
        discarded_completion_tokens = 0
        discarded_cost = 0.0
        # Retry only the provider round-trip plus the content-filter check on
        # its response, and only for transient errors. ``reraise=True``
        # surfaces the underlying domain error (not a tenacity ``RetryError``)
        # once the budget is spent.
        with timed() as call_sw:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, max=10),
                stop=stop_after_delay(retry_budget),
                retry=retry_if_exception_type(
                    (LLMTimeoutError, LLMRateLimitError, LLMContentPolicyViolationError)
                ),
                before_sleep=before_sleep_log(logger, logging.DEBUG),
                after=after_log(logger, logging.DEBUG),
                reraise=True,
            ):
                with attempt:
                    response = await self._complete_once(
                        system_message=system_message,
                        user_message=user_message,
                        tenant_id=tenant_id,
                        timeout=timeout,
                        response_format=response_format,
                    )
                    content = response.choices[0].message.content
                    if content is None:
                        category, severity = _content_filter_signal(response.choices[0])
                        if category is not None:
                            blocked_usage = response.usage
                            if blocked_usage is not None:
                                discarded_prompt_tokens += blocked_usage.prompt_tokens
                                discarded_completion_tokens += (
                                    blocked_usage.completion_tokens
                                )
                                try:
                                    discarded_cost += completion_cost(
                                        completion_response=response
                                    )
                                except Exception:
                                    logger.error(
                                        "No pricing data for model %s", self._model
                                    )
                            logger.warning(
                                "LLM output blocked by content filter: "
                                "category=%s severity=%s",
                                category,
                                severity,
                            )
                            raise LLMContentPolicyViolationError(
                                "LLM provider rejected the response under "
                                "its content policy",
                                category=category,
                                severity=severity,
                                discarded_prompt_tokens=discarded_prompt_tokens,
                                discarded_completion_tokens=discarded_completion_tokens,
                                discarded_cost=discarded_cost,
                            )
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
                raise LLMResponseParseError(
                    f"LLM response validation failed for {response_model.__name__}"
                ) from exc
        elif issubclass(response_model, str):
            parsed_data = content
        else:
            raise ValueError(
                "The `response_model` is not a string or BaseModel subclass."
            )

        total_prompt_tokens = usage.prompt_tokens + discarded_prompt_tokens
        total_completion_tokens = usage.completion_tokens + discarded_completion_tokens
        total_cost = cost + discarded_cost

        # Per-call latency + usage. All fields here are explicitly safe to log
        # (see docs/operations/observability.md) — no message text, prompt, or
        # response content. DEBUG because hierarchical analysis fans out one of
        # these per chunk plus judges and reduces; INFO would be very chatty.
        logger.debug(
            "LLM call: model=%s latency=%.2fs prompt_tokens=%d "
            "completion_tokens=%d cost=%s",
            response.model,
            call_sw.elapsed_seconds,
            total_prompt_tokens,
            total_completion_tokens,
            total_cost,
        )

        return LLMResponse[T_Response](
            structured=parsed_data,
            model=response.model,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost=total_cost,
        )
