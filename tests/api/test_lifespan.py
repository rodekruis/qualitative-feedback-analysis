"""Tests for the FastAPI lifespan's LLM wiring.

Why: the lifespan is the *infrastructure* half of the composition root, and it
owns the one thing ``build_service_graph`` deliberately does not — wrapping each
LLM client in :class:`TrackingLLMAdapter` so usage and cost are recorded. A
judge client that reached the services unwrapped would work perfectly and
silently bill nothing, which no functional test would catch.

The lifespan is exercised directly rather than through a live server: nothing
it does actually connects to the database (the engine is created lazily and
migrations run in ``entrypoint.sh``), so these tests need no Postgres and stay
in the default suite alongside the code they guard.
"""

from __future__ import annotations

import json

import pytest

from qfa.adapters.tracking_llm import TrackingLLMAdapter
from qfa.api.app import create_app
from qfa.domain.models import LLMResponse
from qfa.domain.ports import LLMPort
from qfa.services.analyze import AnalyzeService
from qfa.services.coding import CodingService
from qfa.services.sensitivity import SensitivityService
from qfa.services.summarize import SummarizeService
from qfa.settings import LLMSettings

JUDGE_ENV_VARS = (
    "JUDGE_LLM_MODEL",
    "JUDGE_LLM_API_KEY",
    "JUDGE_LLM_API_BASE",
    "JUDGE_LLM_API_VERSION",
)


class _RecordingFakeLLM(LLMPort):
    """LLM fake that remembers the settings it was built from."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    async def complete(  # pragma: no cover - never invoked at startup
        self,
        system_message,
        user_message,
        tenant_id,
        response_model=str,
        timeout=20.0,
    ) -> LLMResponse:
        raise AssertionError("No LLM call should happen during startup")


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the minimum environment for the lifespan to compose successfully.

    Also clears ``EMBEDDING_*`` and ``JUDGE_LLM_*`` so a developer's local
    ``.env`` cannot turn the no-judge baseline into a judge-enabled run (or
    make startup load a real embedding model).
    """
    for var in (
        "EMBEDDING_MODEL_PATH",
        "EMBEDDING_TOKENIZER_PATH",
        "EMBEDDING_REVISION_HASH",
        *JUDGE_ENV_VARS,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test-lifespan")
    monkeypatch.setenv("LLM_MODEL", "azure/gpt-5.4")
    monkeypatch.setenv("LLM_API_BASE", "https://res.openai.azure.com/")
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


@pytest.mark.asyncio
async def test_judge_client_is_wrapped_for_usage_tracking(app_env: None, monkeypatch):
    """A configured judge client is wrapped in ``TrackingLLMAdapter`` like the primary.

    Both wrappers must also share the *same* usage repository, so judge and
    generation calls accumulate into one tenant's totals rather than being
    recorded against two disconnected stores.
    """
    monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        orchestrator = app.state.orchestrator

        assert isinstance(orchestrator._llm, TrackingLLMAdapter)
        assert isinstance(orchestrator._judge_llm, TrackingLLMAdapter)
        assert orchestrator._judge_llm is not orchestrator._llm
        assert orchestrator._judge_llm._usage_repo is orchestrator._llm._usage_repo
        assert orchestrator._judge_llm._usage_repo is app.state.usage_repo


@pytest.mark.asyncio
async def test_judge_client_is_built_from_resolved_settings(app_env: None, monkeypatch):
    """The judge client is built from judge overrides merged onto the primary settings.

    The inherited API key is the point: it is what lets an operator enable a
    judge model without provisioning a new Key Vault secret.
    """
    monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
    monkeypatch.setenv("JUDGE_LLM_API_BASE", "https://res.services.ai.azure.com/models")
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        judge_settings = app.state.orchestrator._judge_llm._inner.settings

        assert judge_settings.model == "azure_ai/mistral-medium-2505"
        assert judge_settings.api_base == "https://res.services.ai.azure.com/models"
        # Inherited, not re-declared.
        assert judge_settings.api_key.get_secret_value() == "sk-test-lifespan"


@pytest.mark.asyncio
async def test_no_judge_client_is_built_when_unconfigured(app_env: None):
    """Without ``JUDGE_LLM_MODEL`` the lifespan builds exactly one LLM client.

    Identity of the two tracking wrappers is the assertion: a second client
    would double the startup work and, more importantly, mean the default
    deployment silently changed which model judges its own output.
    """
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        orchestrator = app.state.orchestrator

        assert isinstance(orchestrator._llm, TrackingLLMAdapter)
        assert orchestrator._judge_llm is orchestrator._llm


@pytest.mark.asyncio
async def test_judge_model_alone_is_a_valid_startup_configuration(
    app_env: None, monkeypatch
):
    """Setting only ``JUDGE_LLM_MODEL`` starts cleanly — no new failure mode.

    Because every unset judge field inherits from the primary, a lone model
    override is a complete configuration; requiring a matching key or base
    would have made this a startup crash.
    """
    monkeypatch.setenv("JUDGE_LLM_MODEL", "azure/some-other-deployment")
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        judge_settings = app.state.orchestrator._judge_llm._inner.settings

        assert judge_settings.model == "azure/some-other-deployment"
        assert judge_settings.api_key.get_secret_value() == "sk-test-lifespan"
        assert judge_settings.api_base == "https://res.openai.azure.com/"


@pytest.mark.asyncio
async def test_every_service_is_published_on_app_state(app_env: None) -> None:
    """The lifespan publishes one entry per use-case service on ``app.state``.

    The route providers read one slot each, so a service the lifespan forgets
    to publish is a 500 on that endpoint alone — invisible to every other
    test in this module. The API-level tests fake ``app.state`` wholesale, so
    this is the only place that catches such an omission. Every service must
    share the one tracked LLM client and executor the lifespan built, not
    each hold their own.
    """
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.sensitivity_service, SensitivityService)
        # Built from the same graph, so the tracked LLM reaches it too.
        assert (
            app.state.sensitivity_service._executor is app.state.orchestrator._executor
        )

        coding = app.state.coding_service

        assert isinstance(coding, CodingService)
        assert isinstance(coding._llm, TrackingLLMAdapter)
        # One executor per process, shared: see ADR-017.
        assert coding._executor is app.state.orchestrator._executor

        assert isinstance(app.state.analyze_service, AnalyzeService)
        assert app.state.analyze_service._llm is app.state.orchestrator._llm
        assert app.state.analyze_service._executor is app.state.orchestrator._executor

        summarize = app.state.summarize_service

        assert isinstance(summarize, SummarizeService)
        assert summarize._llm is app.state.orchestrator._llm
        assert summarize._executor is app.state.orchestrator._executor
