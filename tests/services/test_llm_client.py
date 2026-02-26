"""Tests for the OpenAI LLM client adapter."""

from unittest.mock import AsyncMock, MagicMock

import openai
import pytest

from feedback_analysis_backend.domain.errors import (
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from feedback_analysis_backend.domain.models import LLMResponse
from feedback_analysis_backend.services.llm_client import OpenAiLLMClient

MODEL = "gpt-4"
SYSTEM_MSG = "You are a helpful assistant."
USER_MSG = "Summarize the feedback."
TIMEOUT = 30.0
TENANT_ID = "tenant-42"


@pytest.fixture
def mock_response():
    """Build a mock OpenAI Responses API response."""
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50

    response = MagicMock()
    response.output_text = "This is the summary."
    response.model = MODEL
    response.usage = usage
    return response


@pytest.fixture
def mock_client(mock_response):
    """Build a mock AsyncOpenAI client whose responses.create() returns mock_response."""
    client = AsyncMock()
    client.responses.create = AsyncMock(return_value=mock_response)
    return client


@pytest.fixture
def llm_client(mock_client):
    """Build the OpenAiLLMClient under test."""
    return OpenAiLLMClient(client=mock_client, model=MODEL)


class TestOpenAiLLMClientHappyPath:
    @pytest.mark.asyncio
    async def test_returns_llm_response_with_correct_fields(
        self, llm_client, mock_response
    ):
        result = await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        assert isinstance(result, LLMResponse)
        assert result.text == "This is the summary."
        assert result.model == MODEL
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50


class TestOpenAiLLMClientCallParameters:
    @pytest.mark.asyncio
    async def test_store_false_enforced(self, llm_client, mock_client):
        await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        call_kwargs = mock_client.responses.create.call_args.kwargs
        assert call_kwargs["store"] is False

    @pytest.mark.asyncio
    async def test_user_equals_tenant_id(self, llm_client, mock_client):
        await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        call_kwargs = mock_client.responses.create.call_args.kwargs
        assert call_kwargs["user"] == TENANT_ID

    @pytest.mark.asyncio
    async def test_model_passed_correctly(self, llm_client, mock_client):
        await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        call_kwargs = mock_client.responses.create.call_args.kwargs
        assert call_kwargs["model"] == MODEL

    @pytest.mark.asyncio
    async def test_timeout_passed_correctly(self, llm_client, mock_client):
        await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        call_kwargs = mock_client.responses.create.call_args.kwargs
        assert call_kwargs["timeout"] == TIMEOUT

    @pytest.mark.asyncio
    async def test_instructions_and_input(self, llm_client, mock_client):
        await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

        call_kwargs = mock_client.responses.create.call_args.kwargs
        assert call_kwargs["instructions"] == SYSTEM_MSG
        assert call_kwargs["input"] == USER_MSG


class TestOpenAiLLMClientExceptionMapping:
    @pytest.mark.asyncio
    async def test_timeout_error_mapped(self, llm_client, mock_client):
        mock_client.responses.create.side_effect = openai.APITimeoutError(
            request=MagicMock()
        )

        with pytest.raises(LLMTimeoutError):
            await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

    @pytest.mark.asyncio
    async def test_rate_limit_error_mapped(self, llm_client, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        mock_client.responses.create.side_effect = openai.RateLimitError(
            message="rate limited",
            response=mock_response,
            body=None,
        )

        with pytest.raises(LLMRateLimitError):
            await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)

    @pytest.mark.asyncio
    async def test_generic_api_error_mapped(self, llm_client, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_client.responses.create.side_effect = openai.APIError(
            message="server error",
            request=MagicMock(),
            body=None,
        )

        with pytest.raises(LLMError):
            await llm_client.complete(SYSTEM_MSG, USER_MSG, TIMEOUT, TENANT_ID)
