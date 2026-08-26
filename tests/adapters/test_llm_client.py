"""Tests for the LiteLLM client adapter."""

import json
import logging
from math import isnan
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from litellm.exceptions import APIError, BadRequestError, RateLimitError, Timeout
from pydantic import BaseModel, Field
from tenacity import wait_fixed

from qfa.adapters.llm_client import (
    _UNSUPPORTED_SCHEMA_KEYWORDS,
    LiteLLMClient,
    _content_filter_signal,
    _provider_safe_response_format,
)
from qfa.domain.errors import (
    LLMBadRequestError,
    LLMContentPolicyViolationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import LLMResponse

SENTINEL = "LEAK-CANARY-7f3a"

MODEL = "azure/gpt-5.4"
SYSTEM_MSG = "You are a helpful assistant."
USER_MSG = "Summarize the feedback."
TIMEOUT = 2.0
TENANT_ID = "tenant-42"


class _StructuredResponse(BaseModel):
    summary: str


def _make_mock_response():
    """Create a mock LiteLLM completion response."""
    choice = MagicMock()
    choice.message.content = "This is the summary."
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    response = MagicMock()
    response.choices = [choice]
    response.model = MODEL
    response.usage = usage
    return response


def _make_client(**overrides):
    """Create a LiteLLMClient with sensible defaults."""
    defaults = {
        "model": MODEL,
        "api_key": "sk-test",
        "api_base": "",
        "api_version": "",
        "chars_per_token": 4,
        "max_total_tokens": 100_000,
    }
    defaults.update(overrides)
    return LiteLLMClient(**defaults)


class TestLiteLLMClientHappyPath:
    @pytest.mark.asyncio
    async def test_returns_llm_response_with_correct_fields(self):
        mock_response = _make_mock_response()
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                return_value=mock_response,
            ),
            patch(
                "qfa.adapters.llm_client.completion_cost",
                return_value=0.001,
            ),
        ):
            result = await client.complete(
                SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
            )

        assert isinstance(result, LLMResponse)
        assert result.structured == "This is the summary."
        assert result.model == MODEL
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.cost == 0.001


class TestLiteLLMClientCallParameters:
    @pytest.mark.asyncio
    async def test_passes_correct_params(self):
        mock_response = _make_mock_response()
        client = _make_client(
            api_base="https://example.com",
            api_version="2024-01-01",
        )
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                return_value=mock_response,
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.0),
        ):
            await client.complete(SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT)

        call_kwargs = mock_ac.call_args.kwargs
        assert call_kwargs["model"] == MODEL
        assert call_kwargs["api_key"] == "sk-test"
        assert call_kwargs["api_base"] == "https://example.com"
        assert call_kwargs["api_version"] == "2024-01-01"
        assert call_kwargs["user"] == TENANT_ID
        assert call_kwargs["timeout"] == TIMEOUT
        messages = call_kwargs["messages"]
        assert messages[0] == {"role": "system", "content": SYSTEM_MSG}
        assert messages[1] == {"role": "user", "content": USER_MSG}

    @pytest.mark.asyncio
    async def test_empty_api_base_passed_as_none(self):
        mock_response = _make_mock_response()
        client = _make_client(api_base="", api_version="")
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                return_value=mock_response,
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.0),
        ):
            await client.complete(SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT)

        call_kwargs = mock_ac.call_args.kwargs
        assert call_kwargs["api_base"] is None
        assert call_kwargs["api_version"] is None

    @pytest.mark.asyncio
    async def test_structured_response_model_sent_and_parsed(self):
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = '{"summary":"Structured summary."}'
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                return_value=mock_response,
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.0),
        ):
            result = await client.complete(
                SYSTEM_MSG,
                USER_MSG,
                TENANT_ID,
                _StructuredResponse,
                timeout=TIMEOUT,
            )

        call_kwargs = mock_ac.call_args.kwargs
        # response_format is now LiteLLM's converted schema dict (sanitised),
        # not the raw model — so unsupported keywords never reach the provider.
        sent = call_kwargs["response_format"]
        assert sent["type"] == "json_schema"
        assert sent["json_schema"]["name"] == "_StructuredResponse"
        assert isinstance(result.structured, _StructuredResponse)
        assert result.structured.summary == "Structured summary."


class TestLiteLLMClientCostFallback:
    @pytest.mark.asyncio
    async def test_cost_nan_when_pricing_unavailable(self):
        mock_response = _make_mock_response()
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                return_value=mock_response,
            ),
            patch(
                "qfa.adapters.llm_client.completion_cost",
                side_effect=Exception("not found"),
            ),
        ):
            result = await client.complete(
                SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
            )

        assert isnan(result.cost)


class TestLiteLLMClientExceptionMapping:
    @pytest.mark.asyncio
    async def test_timeout_error_mapped(self):
        """litellm.Timeout maps to the domain LLMTimeoutError on a single attempt.

        Why: litellm (not openai) is what acompletion raises; the adapter
        must translate the provider-specific timeout into our domain error so
        callers depend only on qfa.domain.errors. Targets ``_complete_once``
        (one attempt) so the mapping is asserted without driving the retry
        loop in ``complete``.
        """
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=Timeout(
                message="timed out", model=MODEL, llm_provider="azure_ai"
            ),
        ):
            with pytest.raises(LLMTimeoutError):
                await client._complete_once(
                    system_message=SYSTEM_MSG,
                    user_message=USER_MSG,
                    tenant_id=TENANT_ID,
                    timeout=TIMEOUT,
                    response_format=None,
                )

    @pytest.mark.asyncio
    async def test_rate_limit_error_mapped(self):
        """litellm.RateLimitError maps to the domain LLMRateLimitError on one attempt.

        Why: same boundary-translation contract as the timeout case, for the
        429 path the retry loop keys off. Targets ``_complete_once`` so the
        mapping is asserted without retrying.
        """
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=RateLimitError(
                message="rate limited", model=MODEL, llm_provider="azure_ai"
            ),
        ):
            with pytest.raises(LLMRateLimitError):
                await client._complete_once(
                    system_message=SYSTEM_MSG,
                    user_message=USER_MSG,
                    tenant_id=TENANT_ID,
                    timeout=TIMEOUT,
                    response_format=None,
                )

    @pytest.mark.asyncio
    async def test_generic_api_error_mapped(self):
        """A generic litellm.APIError maps to the domain LLMError.

        Why: provider errors that aren't timeout/rate-limit/bad-request fall
        through to the catch-all branch and must surface as our base LLMError.
        """
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=APIError(
                status_code=500,
                message="server error",
                model=MODEL,
                llm_provider="azure_ai",
            ),
        ):
            with pytest.raises(LLMError):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

    @pytest.mark.asyncio
    async def test_content_policy_bad_request_mapped(self):
        """A content-filtered BadRequestError maps to LLMContentPolicyViolationError.

        Why: Azure signals content-policy blocks as a BadRequest whose message
        mentions filtering + the content management policy; that case must be
        distinguishable from other bad requests so callers can handle it.
        """
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=BadRequestError(
                message="The response was filtered due to the content management policy",
                model=MODEL,
                llm_provider="azure_ai",
            ),
        ):
            with pytest.raises(LLMContentPolicyViolationError):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

    @pytest.mark.asyncio
    async def test_generic_bad_request_mapped(self):
        """A non-content-policy BadRequestError maps to plain LLMBadRequestError.

        Why: the content-policy branch must not swallow ordinary 400s; those
        should surface as the generic bad-request domain error.
        """
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=BadRequestError(
                message="invalid model parameter",
                model=MODEL,
                llm_provider="azure_ai",
            ),
        ):
            with pytest.raises(LLMBadRequestError):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

    @pytest.mark.asyncio
    async def test_empty_content_raises(self):
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = ""
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await client.complete(
                SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
            )

        assert result.structured == ""

    @pytest.mark.asyncio
    async def test_missing_usage_raises(self):
        mock_response = _make_mock_response()
        mock_response.usage = None
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            with pytest.raises(LLMError, match="missing usage"):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

    @pytest.mark.asyncio
    async def test_missing_content_raises_llm_error(self):
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = None
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            with pytest.raises(LLMError, match="missing content"):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

    @pytest.mark.asyncio
    async def test_non_string_content_raises_llm_error(self):
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = {"summary": "hello"}
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            with pytest.raises(LLMError, match="content must be a string"):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

    @pytest.mark.asyncio
    async def test_structured_validation_error_mapped_to_llm_error(self):
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = '{"invalid": true}'
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            with pytest.raises(LLMError, match="validation failed"):
                await client.complete(
                    SYSTEM_MSG,
                    USER_MSG,
                    TENANT_ID,
                    _StructuredResponse,
                    timeout=TIMEOUT,
                )


class TestProviderTextContainment:
    """Provider exception text never reaches the domain error (ADR-018)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_factory", "expected_class"),
        [
            (
                lambda: Timeout(
                    message=f"boom {SENTINEL}", model=MODEL, llm_provider="azure_ai"
                ),
                LLMTimeoutError,
            ),
            (
                lambda: RateLimitError(
                    message=f"boom {SENTINEL}", model=MODEL, llm_provider="azure_ai"
                ),
                LLMRateLimitError,
            ),
            (
                lambda: BadRequestError(
                    message=f"boom {SENTINEL}", model=MODEL, llm_provider="azure_ai"
                ),
                LLMBadRequestError,
            ),
            (
                lambda: APIError(
                    status_code=500,
                    message=f"boom {SENTINEL}",
                    model=MODEL,
                    llm_provider="azure_ai",
                ),
                LLMError,
            ),
        ],
    )
    async def test_sentinel_absent_from_domain_error(self, exc_factory, expected_class):
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=exc_factory(),
        ):
            with pytest.raises(expected_class) as excinfo:
                await client._complete_once(
                    system_message=SYSTEM_MSG,
                    user_message=USER_MSG,
                    tenant_id=TENANT_ID,
                    timeout=TIMEOUT,
                    response_format=None,
                )

        err = excinfo.value
        assert SENTINEL not in str(err)
        assert not any(SENTINEL in str(a) for a in err.args)
        assert err.__cause__ is not None

    @pytest.mark.asyncio
    async def test_content_filter_sniff_reads_but_does_not_propagate_sentinel(self):
        """The Azure content-filter sniff inspects the string but never leaks it."""
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=BadRequestError(
                message=(
                    "The response was filtered due to the content "
                    f"management policy {SENTINEL}"
                ),
                model=MODEL,
                llm_provider="azure_ai",
            ),
        ):
            with pytest.raises(LLMContentPolicyViolationError) as excinfo:
                await client._complete_once(
                    system_message=SYSTEM_MSG,
                    user_message=USER_MSG,
                    tenant_id=TENANT_ID,
                    timeout=TIMEOUT,
                    response_format=None,
                )

        err = excinfo.value
        assert SENTINEL not in str(err)
        assert not any(SENTINEL in str(a) for a in err.args)
        assert err.__cause__ is not None

    @pytest.mark.asyncio
    async def test_rate_limit_scalars_from_response_header(self):
        response = httpx.Response(
            429,
            headers={"retry-after": "17"},
            request=httpx.Request("POST", "http://example.com"),
        )
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=RateLimitError(
                message="rate limited",
                model=MODEL,
                llm_provider="azure_ai",
                response=response,
            ),
        ):
            with pytest.raises(LLMRateLimitError) as excinfo:
                await client._complete_once(
                    system_message=SYSTEM_MSG,
                    user_message=USER_MSG,
                    tenant_id=TENANT_ID,
                    timeout=TIMEOUT,
                    response_format=None,
                )

        assert excinfo.value.retry_after == 17
        assert excinfo.value.provider_status == 429

    @pytest.mark.asyncio
    async def test_rate_limit_retry_after_none_without_header(self):
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=RateLimitError(
                message="rate limited", model=MODEL, llm_provider="azure_ai"
            ),
        ):
            with pytest.raises(LLMRateLimitError) as excinfo:
                await client._complete_once(
                    system_message=SYSTEM_MSG,
                    user_message=USER_MSG,
                    tenant_id=TENANT_ID,
                    timeout=TIMEOUT,
                    response_format=None,
                )

        assert excinfo.value.retry_after is None

    @pytest.mark.asyncio
    async def test_pydantic_validation_error_excludes_sentinel_and_payload(self):
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = json.dumps({"invalid": SENTINEL})
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            with pytest.raises(LLMError) as excinfo:
                await client.complete(
                    SYSTEM_MSG,
                    USER_MSG,
                    TENANT_ID,
                    _StructuredResponse,
                    timeout=TIMEOUT,
                )

        err = excinfo.value
        assert str(err) == "LLM response validation failed for _StructuredResponse"
        assert SENTINEL not in str(err)
        assert err.__cause__ is not None

    @pytest.mark.asyncio
    async def test_no_log_record_contains_sentinel(self, caplog):
        """No log record's message contains the sentinel, at DEBUG level.

        This exercises tenacity's ``before_sleep_log``, which interpolates
        ``str(exc)`` of the domain error on each retry — clean only because
        step 2 keeps that string free of provider text. Provider text
        reachable only via the ``exc_info`` traceback is exempt per ADR-018.
        """
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=RateLimitError(
                    message=f"boom {SENTINEL}", model=MODEL, llm_provider="azure_ai"
                ),
            ),
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0.01),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            with pytest.raises(LLMRateLimitError):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=0.01
                )

        assert not any(SENTINEL in record.getMessage() for record in caplog.records)


class TestLiteLLMClientRetry:
    """`complete` retries transient failures up to its budget, then re-raises.

    The retry waits are patched to be instant (``wait_fixed``) so these tests
    assert the retry *behaviour* without sleeping through real backoff.
    """

    @pytest.mark.asyncio
    async def test_retries_transient_timeout_then_succeeds(self):
        """A transient timeout is retried and the subsequent success is returned.

        Why: the whole point of moving the retry into the body — a single
        flaky timeout must not fail the call when a retry would succeed.
        """
        good = _make_mock_response()
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=[
                    Timeout(message="transient", model=MODEL, llm_provider="azure_ai"),
                    good,
                ],
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.0),
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0),
            ),
        ):
            result = await client.complete(
                SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
            )

        assert result.structured == "This is the summary."
        assert mock_ac.call_count == 2  # one failure, one success

    @pytest.mark.asyncio
    async def test_retries_exhausted_reraises_domain_error(self):
        """When every attempt times out, the domain LLMTimeoutError is re-raised.

        Why: ``reraise=True`` must surface the underlying domain error (not a
        tenacity RetryError) once the budget is spent, and the call must make
        more than one attempt. A tiny per-attempt timeout keeps the
        ``stop_after_delay`` budget (3x) small so the loop ends in milliseconds.
        """
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=Timeout(
                    message="always", model=MODEL, llm_provider="azure_ai"
                ),
            ) as mock_ac,
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0.01),
            ),
        ):
            with pytest.raises(LLMTimeoutError):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=0.01
                )

        assert mock_ac.call_count >= 2  # retried before giving up

    @pytest.mark.asyncio
    async def test_bad_request_is_not_retried(self):
        """A non-transient BadRequest fails on the first attempt without retrying.

        Why: ordinary bad requests are deterministic — retrying wastes the
        budget and a held concurrency slot. Content-policy violations are the
        one BadRequest subtype that *is* retried; see
        ``test_retries_content_policy_violation_then_succeeds`` below.
        """
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=BadRequestError(
                    message="invalid model parameter",
                    model=MODEL,
                    llm_provider="azure_ai",
                ),
            ) as mock_ac,
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0),
            ),
        ):
            with pytest.raises(LLMBadRequestError):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

        assert mock_ac.call_count == 1  # not retried

    @pytest.mark.asyncio
    async def test_retries_content_policy_violation_then_succeeds(self):
        """A content-policy rejection is retried and a subsequent success returned.

        Why: Azure's content-filter severity classification is not guaranteed
        deterministic for identical input (#293) — a rejection may clear on a
        later attempt, so it is retried like timeout/rate-limit rather than
        failing immediately like other bad requests.
        """
        good = _make_mock_response()
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=[
                    BadRequestError(
                        message=(
                            "The response was filtered due to the content "
                            "management policy"
                        ),
                        model=MODEL,
                        llm_provider="azure_ai",
                    ),
                    good,
                ],
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.0),
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0),
            ),
        ):
            result = await client.complete(
                SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
            )

        assert result.structured == "This is the summary."
        assert mock_ac.call_count == 2  # one rejection, one success
        # The synchronous BadRequestError path rejects before generation, so
        # the discarded attempt carries no billable usage to fold in.
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50

    @pytest.mark.asyncio
    async def test_content_policy_violation_retries_exhausted_reraises(self):
        """When every attempt is content-filtered, the domain error is re-raised.

        Why: a persistently-blocked input (not just a flaky classification)
        must still surface as ``content_policy_violation`` to the caller, not
        hang or raise a generic tenacity ``RetryError``.
        """
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=BadRequestError(
                    message=(
                        "The response was filtered due to the content management policy"
                    ),
                    model=MODEL,
                    llm_provider="azure_ai",
                ),
            ) as mock_ac,
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0.01),
            ),
        ):
            with pytest.raises(LLMContentPolicyViolationError) as excinfo:
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=0.01
                )

        assert mock_ac.call_count >= 2  # retried before giving up
        # No response object exists on the synchronous rejection path, so
        # there is no billable usage to discard.
        assert excinfo.value.discarded_prompt_tokens == 0
        assert excinfo.value.discarded_completion_tokens == 0
        assert excinfo.value.discarded_cost == 0.0

    @pytest.mark.asyncio
    async def test_missing_content_with_filter_annotation_retries_and_succeeds(self):
        """A 200 response with null content + a flagged category is retried.

        Why: Azure's asynchronous content filter lets the call through and
        blocks the completion after generation, instead of raising a
        BadRequestError — that must be retried the same as the synchronous
        rejection, given the same non-deterministic classification (#293).
        """
        filtered = _make_mock_response()
        filtered.choices[0].message.content = None
        filtered.choices[0].provider_specific_fields = {
            "content_filter_results": {
                "violence": {"filtered": True, "severity": "high"},
            },
        }
        good = _make_mock_response()
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                side_effect=[filtered, good],
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.0),
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0),
            ),
        ):
            result = await client.complete(
                SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
            )

        assert result.structured == "This is the summary."
        assert mock_ac.call_count == 2  # one block, one success
        # Azure's asynchronous filter bills the blocked attempt too — that
        # discarded usage must be folded into the final, successful result.
        assert result.prompt_tokens == 200  # 100 discarded + 100 from success
        assert result.completion_tokens == 100  # 50 discarded + 50 from success

    @pytest.mark.asyncio
    async def test_missing_content_with_filter_annotation_exhausts_retries(self):
        """When every attempt is blocked post-hoc, the classified error is re-raised."""
        filtered = _make_mock_response()
        filtered.choices[0].message.content = None
        filtered.choices[0].provider_specific_fields = {
            "content_filter_results": {
                "self_harm": {"filtered": True, "severity": "high"},
            },
        }
        client = _make_client()
        with (
            patch(
                "qfa.adapters.llm_client.acompletion",
                new_callable=AsyncMock,
                return_value=filtered,
            ) as mock_ac,
            patch("qfa.adapters.llm_client.completion_cost", return_value=0.002),
            patch(
                "qfa.adapters.llm_client.wait_exponential",
                return_value=wait_fixed(0.01),
            ),
        ):
            with pytest.raises(LLMContentPolicyViolationError) as excinfo:
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=0.01
                )

        assert excinfo.value.category == "self_harm"
        assert excinfo.value.severity == "high"
        assert mock_ac.call_count >= 2  # retried before giving up
        # Every attempt was billed and blocked; the exhausted error must carry
        # the sum of all of them, not just the last one, so no spend is lost.
        assert excinfo.value.discarded_prompt_tokens == 100 * mock_ac.call_count
        assert excinfo.value.discarded_completion_tokens == 50 * mock_ac.call_count
        assert excinfo.value.discarded_cost == pytest.approx(0.002 * mock_ac.call_count)

    @pytest.mark.asyncio
    async def test_missing_content_without_filter_annotation_not_retried(self):
        """A null-content response with no filter annotation stays a plain LLMError.

        Why: the generic "missing content" fallback (e.g. a malformed
        provider response unrelated to content policy) must not be
        misclassified as retryable just because content is absent.
        """
        mock_response = _make_mock_response()
        mock_response.choices[0].message.content = None
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_ac:
            with pytest.raises(LLMError, match="missing content"):
                await client.complete(
                    SYSTEM_MSG, USER_MSG, TENANT_ID, str, timeout=TIMEOUT
                )

        assert mock_ac.call_count == 1  # not retried


class TestContentFilterSignal:
    """Unit tests for the structural content-filter annotation reader."""

    def test_no_attribute_returns_none(self):
        choice = MagicMock(spec=[])
        assert _content_filter_signal(choice) == (None, None)

    def test_non_dict_provider_fields_returns_none(self):
        choice = MagicMock()
        choice.provider_specific_fields = "not a dict"
        assert _content_filter_signal(choice) == (None, None)

    def test_non_dict_content_filter_results_returns_none(self):
        choice = MagicMock()
        choice.provider_specific_fields = {"content_filter_results": "not a dict"}
        assert _content_filter_signal(choice) == (None, None)

    def test_no_category_flagged_returns_none(self):
        choice = MagicMock()
        choice.provider_specific_fields = {
            "content_filter_results": {
                "hate": {"filtered": False, "severity": "safe"},
            },
        }
        assert _content_filter_signal(choice) == (None, None)

    def test_flagged_category_returns_category_and_severity(self):
        choice = MagicMock()
        choice.provider_specific_fields = {
            "content_filter_results": {
                "hate": {"filtered": False, "severity": "safe"},
                "violence": {"filtered": True, "severity": "high"},
            },
        }
        assert _content_filter_signal(choice) == ("violence", "high")


class TestProviderSafeResponseFormat:
    """The response_format sanitiser keeps providers from seeing rejected keywords."""

    def test_strips_unsupported_validation_keywords(self):
        """ge/le/min_length/max_length/pattern must not reach the schema.

        Why: Azure AI Mistral rejects JSON-Schema validation keywords (e.g.
        `minimum`). We keep the Field constraints on the model — still enforced
        when the response is parsed — but must strip them from the outgoing
        response_format so structured-output calls don't 400.
        """

        class _Constrained(BaseModel):
            score: float = Field(ge=0.0, le=1.0)
            label: str = Field(min_length=1, max_length=40, pattern=r"^[a-z]+$")

        response_format = _provider_safe_response_format(_Constrained)
        blob = json.dumps(response_format)
        leaked = [k for k in _UNSUPPORTED_SCHEMA_KEYWORDS if f'"{k}"' in blob]
        assert leaked == [], f"unsupported keywords leaked: {leaked}"
        # Structure is preserved: a named json_schema with all properties.
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]["schema"]
        assert set(schema["properties"]) == {"score", "label"}
        assert set(schema["required"]) == {"score", "label"}

    def test_strips_keywords_inside_nested_defs(self):
        """Constraints on nested models (carried in `$defs`) are stripped too.

        Why: a single-level walk would miss them, and nested response models
        (e.g. the sensitivity-analysis list) would still 400 on the provider.
        """

        class _Inner(BaseModel):
            n: int = Field(ge=0)

        class _Outer(BaseModel):
            items: tuple[_Inner, ...] = Field(min_length=1)

        response_format = _provider_safe_response_format(_Outer)
        blob = json.dumps(response_format)
        leaked = [k for k in _UNSUPPORTED_SCHEMA_KEYWORDS if f'"{k}"' in blob]
        assert leaked == [], f"unsupported keywords leaked from $defs: {leaked}"

    def test_does_not_mutate_model_field_constraints(self):
        """Sanitising the outgoing schema leaves the model's own validation intact.

        Why: the whole point of stripping at the boundary is to keep the
        declarative Field constraints (and their OpenAPI docs / parse-time
        enforcement) while only the wire schema loses the keywords.
        """

        class _Constrained(BaseModel):
            score: float = Field(ge=0.0, le=1.0)

        _provider_safe_response_format(_Constrained)
        # The model still rejects out-of-range values after sanitisation ran.
        with pytest.raises(ValueError):
            _Constrained(score=2.0)
