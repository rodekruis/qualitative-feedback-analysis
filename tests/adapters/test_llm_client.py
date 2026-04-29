"""Tests for the LiteLLM client adapter."""

from math import isnan
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest

from qfa.adapters.llm_client import LiteLLMClient
from qfa.domain.errors import LLMError, LLMRateLimitError, LLMTimeoutError
from qfa.domain.models import LLMResponse

MODEL = "azure_ai/mistral-large-2411"
SYSTEM_MSG = "You are a helpful assistant."
USER_MSG = "Summarize the feedback."
TIMEOUT = 30.0
TENANT_ID = "tenant-42"


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
            result = await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        assert isinstance(result, LLMResponse)
        assert result.text == "This is the summary."
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
            await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

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
            await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        call_kwargs = mock_ac.call_args.kwargs
        assert call_kwargs["api_base"] is None
        assert call_kwargs["api_version"] is None


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
            result = await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        assert isnan(result.cost)


class TestLiteLLMClientExceptionMapping:
    @pytest.mark.asyncio
    async def test_timeout_error_mapped(self):
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=openai.APITimeoutError(request=MagicMock()),
        ):
            with pytest.raises(LLMTimeoutError):
                await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

    @pytest.mark.asyncio
    async def test_rate_limit_error_mapped(self):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=openai.RateLimitError(
                message="rate limited", response=mock_resp, body=None
            ),
        ):
            with pytest.raises(LLMRateLimitError):
                await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

    @pytest.mark.asyncio
    async def test_generic_api_error_mapped(self):
        client = _make_client()
        with patch(
            "qfa.adapters.llm_client.acompletion",
            new_callable=AsyncMock,
            side_effect=openai.APIError(
                message="server error", request=MagicMock(), body=None
            ),
        ):
            with pytest.raises(LLMError):
                await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

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
            with pytest.raises(LLMError, match="empty content"):
                await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

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
                await client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)
