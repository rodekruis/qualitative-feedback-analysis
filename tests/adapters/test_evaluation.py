"""Tests for EvaluationPort's two adapters (#354)."""

from unittest.mock import patch

from pydantic import SecretStr

from qfa.adapters.evaluation import LangfuseEvaluationAdapter, NoOpEvaluationAdapter
from qfa.domain.ports import EvaluationPort
from qfa.settings import LangfuseSettings


class TestNoOpEvaluationAdapter:
    def test_is_an_evaluation_port(self) -> None:
        assert isinstance(NoOpEvaluationAdapter(), EvaluationPort)

    def test_record_score_returns_none(self) -> None:
        adapter = NoOpEvaluationAdapter()

        result = adapter.record_score(trace_id="a" * 32, name="faithfulness", value=0.9)

        assert result is None


class TestLangfuseEvaluationAdapter:
    """What this adapter passes to ``Langfuse``, which is patched throughout.

    Not about the ``langfuse`` package's own behaviour.
    """

    def test_is_an_evaluation_port(self) -> None:
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example",
        )
        with patch("qfa.adapters.evaluation.Langfuse"):
            adapter = LangfuseEvaluationAdapter(settings)

        assert isinstance(adapter, EvaluationPort)

    def test_constructs_the_client_from_settings(self) -> None:
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example",
        )

        with patch("qfa.adapters.evaluation.Langfuse") as mock_langfuse:
            LangfuseEvaluationAdapter(settings)

        mock_langfuse.assert_called_once_with(
            public_key="pk-test",
            secret_key="sk-test",
            host="https://langfuse.internal.example",
        )

    def test_record_score_forwards_to_create_score(self) -> None:
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example",
        )
        with patch("qfa.adapters.evaluation.Langfuse") as mock_langfuse:
            adapter = LangfuseEvaluationAdapter(settings)
            client = mock_langfuse.return_value

            adapter.record_score(trace_id="a" * 32, name="quality_score", value=0.87)

        client.create_score.assert_called_once_with(
            trace_id="a" * 32,
            name="quality_score",
            value=0.87,
            data_type="NUMERIC",
        )
