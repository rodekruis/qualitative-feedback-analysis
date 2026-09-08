"""Tests for the FastAPI lifespan's LLM wiring.

Why: the lifespan is the *infrastructure* half of the composition root, and it
owns the one thing ``build_services`` deliberately does not — wrapping each
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
from qfa.api import app as app_module
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
        analyze = app.state.analyze_service

        assert isinstance(analyze._llm, TrackingLLMAdapter)
        assert isinstance(analyze._judge_llm, TrackingLLMAdapter)
        assert analyze._judge_llm is not analyze._llm
        assert analyze._judge_llm._usage_repo is analyze._llm._usage_repo
        assert analyze._judge_llm._usage_repo is app.state.usage_repo


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
        judge_settings = app.state.analyze_service._judge_llm._inner.settings

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
        analyze = app.state.analyze_service

        assert isinstance(analyze._llm, TrackingLLMAdapter)
        assert analyze._judge_llm is analyze._llm


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
        judge_settings = app.state.analyze_service._judge_llm._inner.settings

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
        analyze = app.state.analyze_service

        assert isinstance(analyze, AnalyzeService)
        assert isinstance(analyze._llm, TrackingLLMAdapter)

        assert isinstance(app.state.sensitivity_service, SensitivityService)
        # Built from the same graph, so the tracked LLM reaches it too.
        assert app.state.sensitivity_service._executor is analyze._executor

        coding = app.state.coding_service

        assert isinstance(coding, CodingService)
        assert isinstance(coding._llm, TrackingLLMAdapter)
        assert coding._llm is analyze._llm
        # The *tracked* judge client reaches coding too (#310), so its
        # per-level judge calls are billed rather than silently free.
        assert coding._judge_llm is analyze._judge_llm
        # One executor per process, shared: see ADR-017.
        assert coding._executor is analyze._executor

        summarize = app.state.summarize_service

        assert isinstance(summarize, SummarizeService)
        assert summarize._llm is analyze._llm
        assert summarize._executor is analyze._executor


CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
FAKE_CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.invalid/"
)


@pytest.mark.asyncio
async def test_db_engine_is_instrumented_when_telemetry_is_configured(
    app_env: None, monkeypatch
):
    """The DB engine is instrumented here because it is created here.

    Why the lifespan and not ``qfa.telemetry``: the engine does not exist until
    the lifespan builds it, and the instrumentation must target that specific
    instance rather than patching ``create_async_engine`` globally (which
    ``qfa.adapters.db`` from-imports, so the patch would miss). Without this,
    ``AppDependencies`` has no Postgres rows and the Application Map cannot
    render (#223).
    """
    monkeypatch.setenv(CONNECTION_STRING_ENV, FAKE_CONNECTION_STRING)
    created: list[object] = []
    real_create = app_module.create_async_engine_from_settings

    def _recording_create(db_settings):
        engine = real_create(db_settings)
        created.append(engine)
        return engine

    monkeypatch.setattr(
        app_module, "create_async_engine_from_settings", _recording_create
    )
    instrumented: list[object] = []
    monkeypatch.setattr(
        app_module, "instrument_db_engine", lambda engine: instrumented.append(engine)
    )
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        # The engine actually wired into the session factory, not a copy.
        assert len(created) == 1
        assert instrumented == created


@pytest.mark.asyncio
async def test_db_engine_is_not_instrumented_without_telemetry(
    app_env: None, monkeypatch
):
    """No connection string means no exporter, so instrumenting is pure cost.

    Local dev and the test suite both run in this state.
    """
    monkeypatch.delenv(CONNECTION_STRING_ENV, raising=False)
    instrumented: list[object] = []
    monkeypatch.setattr(
        app_module, "instrument_db_engine", lambda engine: instrumented.append(engine)
    )
    app = create_app(llm_factory=_RecordingFakeLLM)

    async with app.router.lifespan_context(app):
        assert instrumented == []
