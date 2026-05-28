"""Tests for the orchestrator composition factory."""

from __future__ import annotations

import json

import pytest

from qfa.adapters.llm_client import LiteLLMClient
from qfa.adapters.presidio_anonymizer import PresidioAnonymizer
from qfa.api.composition import build_orchestrator
from qfa.services.orchestrator import Orchestrator
from qfa.settings import AppSettings


class _StubLLM:
    """Minimal LLMPort stand-in: only exists to be identity-checked."""

    async def complete(  # pragma: no cover - never invoked in these tests
        self,
        system_message,
        user_message,
        tenant_id,
        response_model=str,
        timeout=20.0,
    ):
        raise AssertionError("LLM should not be called during construction")


class _StubEmbedder:
    """Minimal EmbeddingPort stand-in for identity-check tests."""

    def embed(self, texts):  # pragma: no cover - never invoked here
        raise AssertionError("Embedder should not be called during construction")


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide just enough env to construct ``AppSettings()``.

    ``AuthSettings.api_keys`` is required and has no default; we set
    a single fake tenant key so ``AppSettings()`` instantiates cleanly
    without touching the LLM, DB, or embedding settings.
    """
    monkeypatch.setenv("LLM_API_KEY", "sk-test-composition")
    # DatabaseSettings requires DB_HOST when DB_URL is unset; the
    # factory doesn't touch the DB but ``AppSettings()`` validates
    # all sub-settings at construction.
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://t:t@localhost/test")
    monkeypatch.setenv(
        "AUTH_API_KEYS",
        json.dumps(
            [
                {
                    "key_id": "tenant-test-0",
                    "name": "Test tenant",
                    "key": "test-api-key-123456789012",
                    "hashed_key": None,
                    "tenant_id": "tenant-test",
                    "is_superuser": False,
                }
            ]
        ),
    )


class TestBuildOrchestrator:
    """Composition factory wires the orchestrator dependencies correctly."""

    def test_returns_orchestrator_with_default_components(self, auth_env: None) -> None:
        """Without overrides the factory builds a real LLM + Presidio + no embedder.

        Embedder is ``None`` when ``EMBEDDING_MODEL_PATH`` is unset — the
        normal local/CI state — and the orchestrator carries that through
        until ``analyze_hierarchical`` is called.
        """
        settings = AppSettings()

        orchestrator = build_orchestrator(settings)

        assert isinstance(orchestrator, Orchestrator)
        # The default LLM is the real LiteLLM client built from settings.llm.
        # We do not invoke it; we just confirm the factory picked it up.
        assert isinstance(orchestrator._llm, LiteLLMClient)
        assert isinstance(orchestrator._anonymizer, PresidioAnonymizer)
        assert orchestrator._embedder is None
        assert orchestrator._analyze_settings is settings.analyze

    def test_uses_injected_llm(self, auth_env: None) -> None:
        """An ``llm=`` override is plumbed straight into the orchestrator.

        This is how the FastAPI lifespan injects a ``TrackingLLMAdapter``-
        wrapped LLM without the factory needing to know about the DB.
        """
        settings = AppSettings()
        stub_llm = _StubLLM()

        orchestrator = build_orchestrator(settings, llm=stub_llm)

        assert orchestrator._llm is stub_llm

    def test_uses_injected_embedder(self, auth_env: None) -> None:
        """An ``embedder=`` override is plumbed straight into the orchestrator.

        Mirrors the lifespan, which builds the embedder explicitly to log
        its construction at startup and then passes it in.
        """
        settings = AppSettings()
        stub_embedder = _StubEmbedder()

        orchestrator = build_orchestrator(settings, embedder=stub_embedder)

        assert orchestrator._embedder is stub_embedder

    def test_propagates_token_budget_and_timeouts(self, auth_env: None) -> None:
        """LLM-side limits flow from settings.llm into the orchestrator.

        These two knobs (``timeout_seconds``, ``max_total_tokens``) are
        carried on ``LLMSettings`` but consumed by the orchestrator, so the
        factory has to bridge them explicitly. A regression here would
        silently cap the wrong chunk size — guard it.
        """
        settings = AppSettings()
        # Confirm the factory reads these from settings.llm, not elsewhere.
        expected_timeout = settings.llm.timeout_seconds
        expected_max_tokens = settings.llm.max_total_tokens

        orchestrator = build_orchestrator(settings, llm=_StubLLM())

        assert orchestrator._llm_timeout_seconds == expected_timeout
        assert orchestrator._max_total_tokens == expected_max_tokens
