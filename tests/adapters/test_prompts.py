"""Tests for PromptPort's two adapters (#398)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langfuse.api import NotFoundError
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr

from qfa.adapters.prompts import LangfusePromptAdapter, NoOpPromptAdapter
from qfa.domain.ports import PromptPort
from qfa.settings import LangfuseSettings


def _settings() -> LangfuseSettings:
    return LangfuseSettings(
        public_key="pk-test",
        secret_key=SecretStr("sk-test"),
        host="https://langfuse.internal.example",
    )


def _existing_prompt(prompt: str, version: int) -> SimpleNamespace:
    """A stand-in for the ``TextPromptClient`` ``get_prompt`` returns.

    Only ``.prompt`` and ``.version`` are read by the adapter.
    """
    return SimpleNamespace(prompt=prompt, version=version)


class TestNoOpPromptAdapter:
    def test_declares_prompt_port_as_a_base(self) -> None:
        """Explicit inheritance per AGENTS.md's port-inheritance rule.

        ``PromptPort`` is a plain (not ``@runtime_checkable``) ``Protocol``,
        like ``LLMPort`` — ``isinstance``/``issubclass`` against it raise, so
        this checks the MRO directly instead.
        """
        assert PromptPort in NoOpPromptAdapter.__mro__

    @pytest.mark.asyncio
    async def test_sync_returns_empty_dict_for_any_input(self) -> None:
        adapter = NoOpPromptAdapter()

        result = await adapter.sync({"analyze-judge": "some prompt text"})

        assert result == {}

    @pytest.mark.asyncio
    async def test_sync_returns_empty_dict_for_empty_input(self) -> None:
        adapter = NoOpPromptAdapter()

        result = await adapter.sync({})

        assert result == {}


class TestLangfusePromptAdapter:
    """What this adapter passes to ``Langfuse``, which is patched throughout.

    Not about the ``langfuse`` package's own behaviour.
    """

    def test_declares_prompt_port_as_a_base(self) -> None:
        """See ``TestNoOpPromptAdapter``'s equivalent test for why MRO, not isinstance."""
        assert PromptPort in LangfusePromptAdapter.__mro__

    def test_constructs_the_client_from_settings(self) -> None:
        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            LangfusePromptAdapter(_settings())

        mock_langfuse.assert_called_once()
        kwargs = mock_langfuse.call_args.kwargs
        assert kwargs["public_key"] == "pk-test"
        assert kwargs["secret_key"] == "sk-test"
        assert kwargs["host"] == "https://langfuse.internal.example"
        assert isinstance(kwargs["tracer_provider"], TracerProvider)

    def test_uses_its_own_tracer_provider_not_the_global_one(self) -> None:
        """Never the process-global provider Application Insights may install.

        Mirrors ``LangfuseEvaluationAdapter``'s own guard against the same
        leak (see ``tests/adapters/test_evaluation.py``).
        """
        global_provider = TracerProvider()

        with (
            patch(
                "opentelemetry.trace.get_tracer_provider",
                return_value=global_provider,
            ),
            patch("qfa.adapters.prompts.Langfuse") as mock_langfuse,
        ):
            LangfusePromptAdapter(_settings())

        assert mock_langfuse.call_args.kwargs["tracer_provider"] is not global_provider

    @pytest.mark.asyncio
    async def test_pushes_a_new_name_that_does_not_exist_yet(self) -> None:
        """``get_prompt`` raising ``NotFoundError`` means "create it"."""
        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            client = mock_langfuse.return_value
            client.get_prompt.side_effect = NotFoundError(body={"message": "not found"})
            client.create_prompt.return_value = _existing_prompt("new text", version=1)
            adapter = LangfusePromptAdapter(_settings())

            result = await adapter.sync({"analyze-judge": "new text"})

        client.create_prompt.assert_called_once_with(
            name="analyze-judge", prompt="new text", labels=["production"]
        )
        assert result == {"analyze-judge": 1}

    @pytest.mark.asyncio
    async def test_does_not_push_a_name_whose_text_is_unchanged(self) -> None:
        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            client = mock_langfuse.return_value
            client.get_prompt.return_value = _existing_prompt("same text", version=3)
            adapter = LangfusePromptAdapter(_settings())

            result = await adapter.sync({"analyze-judge": "same text"})

        client.create_prompt.assert_not_called()
        assert result == {"analyze-judge": 3}

    @pytest.mark.asyncio
    async def test_pushes_a_name_whose_text_changed(self) -> None:
        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            client = mock_langfuse.return_value
            client.get_prompt.return_value = _existing_prompt("old text", version=3)
            client.create_prompt.return_value = _existing_prompt("new text", version=4)
            adapter = LangfusePromptAdapter(_settings())

            result = await adapter.sync({"analyze-judge": "new text"})

        client.create_prompt.assert_called_once_with(
            name="analyze-judge", prompt="new text", labels=["production"]
        )
        assert result == {"analyze-judge": 4}

    @pytest.mark.asyncio
    async def test_uses_the_production_label_for_the_lookup(self) -> None:
        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            client = mock_langfuse.return_value
            client.get_prompt.return_value = _existing_prompt("text", version=1)
            adapter = LangfusePromptAdapter(_settings())

            await adapter.sync({"analyze-judge": "text"})

        client.get_prompt.assert_called_once_with("analyze-judge", label="production")

    @pytest.mark.asyncio
    async def test_one_names_failure_does_not_stop_the_others(self) -> None:
        """An unrelated exception on one name is swallowed; the rest still sync."""

        def _get_prompt(name: str, **kwargs: object) -> SimpleNamespace:
            if name == "broken-prompt":
                raise RuntimeError("connection reset")
            return _existing_prompt("text", version=1)

        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            client = mock_langfuse.return_value
            client.get_prompt.side_effect = _get_prompt
            adapter = LangfusePromptAdapter(_settings())

            result = await adapter.sync(
                {"broken-prompt": "text", "healthy-prompt": "text"}
            )

        assert result == {"healthy-prompt": 1}

    @pytest.mark.asyncio
    async def test_failure_log_never_carries_the_prompt_text(self, caplog) -> None:
        """The warning names the prompt only by its Langfuse name, never its text."""
        secret_text = "SECRET-PROMPT-TEXT-CANARY"

        with patch("qfa.adapters.prompts.Langfuse") as mock_langfuse:
            client = mock_langfuse.return_value
            client.get_prompt.side_effect = RuntimeError(secret_text)
            adapter = LangfusePromptAdapter(_settings())

            with caplog.at_level("WARNING", logger="qfa.adapters.prompts"):
                result = await adapter.sync({"broken-prompt": secret_text})

        assert result == {}
        for record in caplog.records:
            assert secret_text not in record.getMessage()
