"""Tests for the application-service composition factory."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr

from qfa.adapters.llm_client import LiteLLMClient
from qfa.adapters.presidio_anonymizer import PresidioAnonymizer
from qfa.api.composition import (
    build_orchestrator,
    build_services,
    resolve_judge_llm_settings,
)
from qfa.services.coding import CodingService
from qfa.services.orchestrator import Orchestrator
from qfa.services.sensitivity import SensitivityService
from qfa.settings import AppSettings, JudgeLLMSettings, LLMSettings

JUDGE_ENV_VARS = (
    "JUDGE_LLM_MODEL",
    "JUDGE_LLM_API_KEY",
    "JUDGE_LLM_API_BASE",
    "JUDGE_LLM_API_VERSION",
)


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
    without touching the LLM, DB, or embedding settings. We also clear any
    ``EMBEDDING_*`` leaking from a developer's local ``.env`` (loaded via
    direnv) so the default-components test deterministically sees the
    no-embedder state rather than building a real embedder.
    """
    for embedding_var in (
        "EMBEDDING_MODEL_PATH",
        "EMBEDDING_TOKENIZER_PATH",
        "EMBEDDING_REVISION_HASH",
    ):
        monkeypatch.delenv(embedding_var, raising=False)
    # Likewise clear any ambient judge configuration, so the default tests
    # below see the no-judge-client state rather than a locally-enabled one.
    for judge_var in JUDGE_ENV_VARS:
        monkeypatch.delenv(judge_var, raising=False)
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


@pytest.fixture
def primary_llm_settings() -> LLMSettings:
    """A fully-populated primary connection, so inheritance is observable per field."""
    return LLMSettings(
        api_key=SecretStr("sk-primary"),
        model="azure/gpt-5.4",
        api_base="https://res.openai.azure.com/",
        api_version="2024-05-01-preview",
    )


class TestResolveJudgeLLMSettings:
    """The judge/primary per-field inheritance rule, applied once at composition."""

    def test_returns_none_when_no_judge_model_is_configured(
        self, primary_llm_settings: LLMSettings
    ) -> None:
        """An unset ``JUDGE_LLM_MODEL`` means "no separate judge connection".

        This is the default state, and the reason the change is behaviour-
        preserving out of the box: no second client is built, so judge calls
        stay on the primary one.
        """
        assert (
            resolve_judge_llm_settings(primary_llm_settings, JudgeLLMSettings()) is None
        )

    def test_treats_an_empty_judge_model_as_unset(
        self, primary_llm_settings: LLMSettings
    ) -> None:
        """``JUDGE_LLM_MODEL=`` disables the judge connection rather than half-enabling it.

        An empty model string could never route anywhere, so accepting it
        would only trade a clear no-op for a startup failure.
        """
        resolved = resolve_judge_llm_settings(
            primary_llm_settings, JudgeLLMSettings(model="")
        )

        assert resolved is None

    def test_model_alone_inherits_every_credential_field(
        self, primary_llm_settings: LLMSettings
    ) -> None:
        """One non-secret variable is enough to enable a judge model.

        This is the acceptance criterion that keeps Key Vault out of the
        picture: with only ``JUDGE_LLM_MODEL`` set, the API key, base URL and
        version all come from the primary ``LLM_*`` connection, so no new
        secret has to be provisioned in any environment.
        """
        resolved = resolve_judge_llm_settings(
            primary_llm_settings,
            JudgeLLMSettings(model="azure/some-other-deployment"),
        )

        assert resolved is not None
        assert resolved.model == "azure/some-other-deployment"
        assert resolved.api_key.get_secret_value() == "sk-primary"
        assert resolved.api_base == "https://res.openai.azure.com/"
        assert resolved.api_version == "2024-05-01-preview"

    @pytest.mark.parametrize(
        ("field", "override", "expected"),
        [
            ("api_key", SecretStr("sk-judge"), "sk-judge"),
            ("api_base", "https://res.services.ai.azure.com/models", None),
            ("api_version", "2099-01-01-preview", None),
        ],
    )
    def test_each_field_overrides_only_itself(
        self,
        primary_llm_settings: LLMSettings,
        field: str,
        override: Any,
        expected: str | None,
    ) -> None:
        """Setting one ``JUDGE_LLM_*`` field leaves the other fields inherited.

        Parametrised per field because the failure mode this guards is
        asymmetric: a resolution bug is likely to affect one field (or to
        reset the others to their defaults) rather than all of them at once.
        """
        only_this_field: dict[str, Any] = {field: override}
        judge = JudgeLLMSettings(
            model="azure_ai/mistral-medium-2505", **only_this_field
        )

        resolved = resolve_judge_llm_settings(primary_llm_settings, judge)

        assert resolved is not None
        assert resolved.model == "azure_ai/mistral-medium-2505"
        # The overridden field took the judge value...
        if field == "api_key":
            assert resolved.api_key.get_secret_value() == expected
        else:
            assert getattr(resolved, field) == override
        # ...and every field that was left unset still mirrors the primary.
        for other in ("api_key", "api_base", "api_version"):
            if other == field:
                continue
            assert getattr(resolved, other) == getattr(primary_llm_settings, other)

    def test_inherits_the_shared_budget_knobs(
        self, primary_llm_settings: LLMSettings
    ) -> None:
        """``timeout_seconds``/``max_total_tokens``/``chars_per_token`` have no judge override.

        The judge block deliberately exposes only connection fields; the
        budget knobs stay single-sourced on the primary settings so the two
        clients cannot drift apart on timeout or token accounting.
        """
        primary = primary_llm_settings.model_copy(
            update={
                "timeout_seconds": 42.0,
                "max_total_tokens": 1234,
                "chars_per_token": 7,
            }
        )

        resolved = resolve_judge_llm_settings(
            primary, JudgeLLMSettings(model="azure/judge")
        )

        assert resolved is not None
        assert resolved.timeout_seconds == 42.0
        assert resolved.max_total_tokens == 1234
        assert resolved.chars_per_token == 7

    def test_does_not_mutate_the_primary_settings(
        self, primary_llm_settings: LLMSettings
    ) -> None:
        """Resolution produces a new object; the primary connection is untouched.

        The judge client must be built *alongside* the primary one, never by
        rewriting it — otherwise enabling a judge would silently move
        generation onto the judge model too.
        """
        resolve_judge_llm_settings(
            primary_llm_settings,
            JudgeLLMSettings(
                model="azure_ai/mistral-medium-2505",
                api_base="https://res.services.ai.azure.com/models",
            ),
        )

        assert primary_llm_settings.model == "azure/gpt-5.4"
        assert primary_llm_settings.api_base == "https://res.openai.azure.com/"


class TestBuildOrchestratorJudgeClient:
    """The factory builds and injects a judge client only when one is configured."""

    def test_judge_calls_use_the_primary_client_by_default(
        self, auth_env: None
    ) -> None:
        """Without ``JUDGE_LLM_MODEL`` the orchestrator holds one client for everything.

        Identity (``is``), not equality: the point is that no second client
        exists at all, so behaviour and cost are byte-for-byte what they were
        before the judge connection was added.
        """
        orchestrator = build_orchestrator(AppSettings(), llm=_StubLLM())

        assert orchestrator._judge_llm is orchestrator._llm

    def test_builds_a_distinct_judge_client_when_a_judge_model_is_set(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured judge model yields a second client, leaving the primary intact.

        The injected primary must survive untouched — a judge client that
        replaced rather than accompanied it would move generation onto the
        judge model.
        """
        monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
        stub_llm = _StubLLM()

        orchestrator = build_orchestrator(AppSettings(), llm=stub_llm)

        assert orchestrator._llm is stub_llm
        assert orchestrator._judge_llm is not stub_llm
        assert isinstance(orchestrator._judge_llm, LiteLLMClient)
        assert orchestrator._judge_llm._model == "azure_ai/mistral-medium-2505"
        # The inherited credential is what makes this a no-new-secret change.
        assert orchestrator._judge_llm._api_key == "sk-test-composition"

    def test_uses_injected_judge_llm(self, auth_env: None) -> None:
        """A ``judge_llm=`` override is plumbed straight through, like ``llm=``.

        This is the seam the FastAPI lifespan uses to hand in a judge client
        already wrapped in ``TrackingLLMAdapter``.
        """
        stub_judge = _StubLLM()

        orchestrator = build_orchestrator(
            AppSettings(), llm=_StubLLM(), judge_llm=stub_judge
        )

        assert orchestrator._judge_llm is stub_judge

    def test_injected_judge_llm_wins_over_configuration(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``judge_llm=`` suppresses building one from settings.

        Without this the lifespan would construct the judge client twice —
        once wrapped for tracking, once bare — and the bare one would win.
        """
        monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
        stub_judge = _StubLLM()

        orchestrator = build_orchestrator(
            AppSettings(), llm=_StubLLM(), judge_llm=stub_judge
        )

        assert orchestrator._judge_llm is stub_judge


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


class TestBuildServices:
    """The factory returns every service, wired over one shared executor."""

    def test_returns_every_service(self, auth_env: None) -> None:
        """One graph, one field per service the request lifecycle can reach."""
        services = build_services(AppSettings(), llm=_StubLLM())

        assert isinstance(services.orchestrator, Orchestrator)
        assert isinstance(services.sensitivity, SensitivityService)
        assert isinstance(services.coding, CodingService)

    def test_services_share_one_executor_and_anonymizer(self, auth_env: None) -> None:
        """Identity, not equality: a second executor is the failure to catch.

        Per ADR-017 the executor is where the token ceiling and per-call
        timeout are bound. Two instances would let a service drift onto a
        stale budget while still looking correctly wired.
        """
        services = build_services(AppSettings(), llm=_StubLLM())

        assert services.sensitivity._executor is services.orchestrator._executor
        assert services.coding._executor is services.orchestrator._executor
        assert services.coding._anonymizer is services.orchestrator._anonymizer

    def test_coding_service_runs_on_the_primary_connection(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured judge model does not reach the coding path.

        #258 scoped the judge split to the analyse/summarise quality judges,
        and the coding service takes no judge client at all — so even with
        ``JUDGE_LLM_MODEL`` set it must still hold the primary.
        """
        monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
        stub_llm = _StubLLM()

        services = build_services(AppSettings(), llm=stub_llm)

        assert services.coding._llm is stub_llm
        assert services.orchestrator._judge_llm is not stub_llm

    def test_build_orchestrator_returns_the_graph_s_orchestrator(
        self, auth_env: None
    ) -> None:
        """The narrow entry point is the same construction, minus the graph.

        Scripts and notebooks keep calling ``build_orchestrator``; it must
        stay a view onto ``build_services`` rather than a second wiring path
        that can drift.
        """
        stub_llm = _StubLLM()

        orchestrator = build_orchestrator(AppSettings(), llm=stub_llm)

        assert isinstance(orchestrator, Orchestrator)
        assert orchestrator._llm is stub_llm
