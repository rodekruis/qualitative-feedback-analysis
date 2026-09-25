"""Tests for the application-service composition factory."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import litellm
import pytest
from pydantic import SecretStr, ValidationError

from qfa.adapters.evaluation import NoOpEvaluationAdapter
from qfa.adapters.llm_client import LiteLLMClient
from qfa.adapters.presidio_anonymizer import PresidioAnonymizer
from qfa.api.composition import (
    build_analyze_service,
    build_evaluator,
    build_langfuse_tracer,
    build_prompt_versions,
    build_services,
    register_custom_model_prices,
    resolve_judge_llm_settings,
)
from qfa.domain.ports import PromptPort
from qfa.services.analyze import AnalyzeService
from qfa.services.coding import CodingService
from qfa.services.sensitivity import SensitivityService
from qfa.services.summarize import SummarizeService
from qfa.settings import AppSettings, JudgeLLMSettings, LangfuseSettings, LLMSettings

JUDGE_ENV_VARS = (
    "JUDGE_LLM_MODEL",
    "JUDGE_LLM_API_KEY",
    "JUDGE_LLM_API_BASE",
    "JUDGE_LLM_API_VERSION",
)

LANGFUSE_ENV_VARS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


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


class _StubEvaluator:
    """Minimal EvaluationPort stand-in for identity-check tests."""

    def record_score(self, *, trace_id, name, value):  # pragma: no cover
        raise AssertionError("Evaluator should not be called during construction")


class _StubPromptPort(PromptPort):
    """Minimal PromptPort stand-in for identity-check tests (#398)."""

    async def sync(self, prompts):  # pragma: no cover - never invoked here
        raise AssertionError("PromptPort should not be called during construction")


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
    # And any ambient Langfuse configuration: a partial one (e.g. a public
    # key with no host, from a developer's local .env) would otherwise fail
    # AppSettings() validation here for reasons unrelated to what these
    # tests check.
    for langfuse_var in LANGFUSE_ENV_VARS:
        monkeypatch.delenv(langfuse_var, raising=False)
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


class TestBuildAnalyzeServiceJudgeClient:
    """The factory builds and injects a judge client only when one is configured."""

    @pytest.mark.asyncio
    async def test_judge_calls_use_the_primary_client_by_default(
        self, auth_env: None
    ) -> None:
        """Without ``JUDGE_LLM_MODEL`` the service holds one client for everything.

        Identity (``is``), not equality: the point is that no second client
        exists at all, so behaviour and cost are byte-for-byte what they were
        before the judge connection was added.
        """
        analyze = await build_analyze_service(AppSettings(), llm=_StubLLM())

        assert analyze._judge_llm is analyze._llm

    @pytest.mark.asyncio
    async def test_builds_a_distinct_judge_client_when_a_judge_model_is_set(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured judge model yields a second client, leaving the primary intact.

        The injected primary must survive untouched — a judge client that
        replaced rather than accompanied it would move generation onto the
        judge model.
        """
        monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
        stub_llm = _StubLLM()

        analyze = await build_analyze_service(AppSettings(), llm=stub_llm)

        assert analyze._llm is stub_llm
        assert analyze._judge_llm is not stub_llm
        assert isinstance(analyze._judge_llm, LiteLLMClient)
        assert analyze._judge_llm._model == "azure_ai/mistral-medium-2505"
        # The inherited credential is what makes this a no-new-secret change.
        assert analyze._judge_llm._api_key == "sk-test-composition"

    @pytest.mark.asyncio
    async def test_uses_injected_judge_llm(self, auth_env: None) -> None:
        """A ``judge_llm=`` override is plumbed straight through, like ``llm=``.

        This is the seam the FastAPI lifespan uses to hand in a judge client
        already wrapped in ``TrackingLLMAdapter``.
        """
        stub_judge = _StubLLM()

        analyze = await build_analyze_service(
            AppSettings(), llm=_StubLLM(), judge_llm=stub_judge
        )

        assert analyze._judge_llm is stub_judge

    @pytest.mark.asyncio
    async def test_injected_judge_llm_wins_over_configuration(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``judge_llm=`` suppresses building one from settings.

        Without this the lifespan would construct the judge client twice —
        once wrapped for tracking, once bare — and the bare one would win.
        """
        monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
        stub_judge = _StubLLM()

        analyze = await build_analyze_service(
            AppSettings(), llm=_StubLLM(), judge_llm=stub_judge
        )

        assert analyze._judge_llm is stub_judge


class TestBuildAnalyzeService:
    """Composition factory wires the analyze service dependencies correctly."""

    @pytest.mark.asyncio
    async def test_returns_analyze_service_with_default_components(
        self, auth_env: None
    ) -> None:
        """Without overrides the factory builds a real LLM + Presidio + no embedder.

        Embedder is ``None`` when ``EMBEDDING_MODEL_PATH`` is unset — the
        normal local/CI state — and the service carries that through until
        ``analyze_hierarchical`` is called, which then raises.
        """
        settings = AppSettings()

        analyze = await build_analyze_service(settings)

        assert isinstance(analyze, AnalyzeService)
        assert isinstance(analyze._llm, LiteLLMClient)
        assert isinstance(analyze._anonymizer, PresidioAnonymizer)
        assert analyze._embedder is None
        assert analyze._analyze_settings is settings.analyze

    @pytest.mark.asyncio
    async def test_uses_injected_embedder(self, auth_env: None) -> None:
        """An ``embedder=`` override is plumbed straight into the analyze service.

        Mirrors the lifespan, which builds the embedder explicitly to log
        its construction at startup and then passes it in.
        """
        settings = AppSettings()
        stub_embedder = _StubEmbedder()

        analyze = await build_analyze_service(settings, embedder=stub_embedder)

        assert analyze._embedder is stub_embedder

    @pytest.mark.asyncio
    async def test_uses_injected_llm_and_judge_llm(self, auth_env: None) -> None:
        """``llm=`` / ``judge_llm=`` overrides reach the analyze service too."""
        stub_llm = _StubLLM()
        stub_judge = _StubLLM()

        analyze = await build_analyze_service(
            AppSettings(), llm=stub_llm, judge_llm=stub_judge
        )

        assert analyze._llm is stub_llm
        assert analyze._judge_llm is stub_judge

    @pytest.mark.asyncio
    async def test_propagates_token_budget(self, auth_env: None) -> None:
        """``max_total_tokens`` flows from settings.llm into the analyze service.

        It sizes the map chunks and reduce groups, so a regression here would
        silently cap the wrong chunk size — guard it.
        """
        settings = AppSettings()

        analyze = await build_analyze_service(settings, llm=_StubLLM())

        assert analyze._max_total_tokens == settings.llm.max_total_tokens


class TestBuildServices:
    """The factory builds every service over one shared executor and anonymiser."""

    @pytest.mark.asyncio
    async def test_returns_every_service(self, auth_env: None) -> None:
        """One graph, one field per service the request lifecycle can reach."""
        services = await build_services(AppSettings(), llm=_StubLLM())

        assert isinstance(services.sensitivity, SensitivityService)
        assert isinstance(services.coding, CodingService)
        assert isinstance(services.analyze, AnalyzeService)
        assert isinstance(services.summarize, SummarizeService)

    @pytest.mark.asyncio
    async def test_every_service_shares_the_one_executor(self, auth_env: None) -> None:
        """Identity, not equality: a second executor is the failure to catch.

        Per ADR-017 the executor is where the token ceiling and per-call
        timeout are bound. Two instances would let a service drift onto a
        stale budget while still looking correctly wired.
        """
        services = await build_services(AppSettings(), llm=_StubLLM())

        assert services.coding._executor is services.sensitivity._executor
        assert services.analyze._executor is services.sensitivity._executor
        assert services.summarize._executor is services.sensitivity._executor

    @pytest.mark.asyncio
    async def test_every_service_shares_the_one_anonymiser(
        self, auth_env: None
    ) -> None:
        """Constructing ``PresidioAnonymizer`` loads spaCy models; do it once."""
        services = await build_services(AppSettings(), llm=_StubLLM())

        assert services.coding._anonymizer is services.analyze._anonymizer
        assert services.summarize._anonymizer is services.analyze._anonymizer

    @pytest.mark.asyncio
    async def test_no_judge_model_leaves_every_judge_on_the_primary(
        self, auth_env: None
    ) -> None:
        """The default path: unset ``JUDGE_LLM_MODEL`` means one client each."""
        services = await build_services(AppSettings(), llm=_StubLLM())

        assert services.coding._judge_llm is services.coding._llm
        assert services.analyze._judge_llm is services.analyze._llm
        assert services.summarize._judge_llm is services.summarize._llm

    @pytest.mark.asyncio
    async def test_shared_executor_gets_timeout_and_token_budget_from_settings(
        self, auth_env: None
    ) -> None:
        """LLM-side limits flow from settings.llm into the shared executor.

        These two knobs (``timeout_seconds``, ``max_total_tokens``) are
        carried on ``LLMSettings`` but consumed by the executor every
        service delegates to, so the factory has to bridge them explicitly.
        A regression here would silently cap the wrong chunk size — guard it.
        """
        settings = AppSettings()

        services = await build_services(settings, llm=_StubLLM())

        assert services.analyze._executor._llm_timeout_seconds == (
            settings.llm.timeout_seconds
        )
        assert services.analyze._executor._max_total_tokens == (
            settings.llm.max_total_tokens
        )

    @pytest.mark.asyncio
    async def test_anonymizer_gets_worker_count_and_batch_size_from_settings(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ANONYMIZATION_*`` settings must reach the shared ``PresidioAnonymizer``.

        A regression here would silently strand the settings unused and
        leave every deployment on the hardcoded default regardless of
        configuration — including the ``ANONYMIZATION_MAX_WORKERS=1`` kill
        switch.
        """
        monkeypatch.setenv("ANONYMIZATION_MAX_WORKERS", "1")
        monkeypatch.setenv("ANONYMIZATION_BATCH_SIZE", "32")
        settings = AppSettings()

        services = await build_services(settings, llm=_StubLLM())

        anonymizer = services.analyze._anonymizer
        assert isinstance(anonymizer, PresidioAnonymizer)
        assert anonymizer._max_workers == 1
        assert anonymizer._batch_size == 32

    @pytest.mark.asyncio
    async def test_summarize_service_gets_both_connections(
        self, auth_env: None
    ) -> None:
        """Generation and judge clients reach the summarisation service.

        Its two judge call sites moved out of the old ``Orchestrator`` god
        class with #264, so a graph that dropped ``judge_llm`` here would
        silently move them back onto the generation model.
        """
        stub_llm = _StubLLM()
        stub_judge = _StubLLM()

        services = await build_services(
            AppSettings(), llm=stub_llm, judge_llm=stub_judge
        )

        assert services.summarize._llm is stub_llm
        assert services.summarize._judge_llm is stub_judge

    @pytest.mark.asyncio
    async def test_coding_service_gets_the_judge_connection(
        self, auth_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#310 routes coding's per-level judge onto the judge connection too.

        Generation must stay put — the one-shot pick is not a judge call — and
        the judge client has to be the *same* instance the analyse service
        holds: one judge connection per process, as with the executor.
        """
        monkeypatch.setenv("JUDGE_LLM_MODEL", "azure_ai/mistral-medium-2505")
        stub_llm = _StubLLM()

        services = await build_services(AppSettings(), llm=stub_llm)

        assert services.coding._llm is stub_llm
        assert services.coding._judge_llm is not stub_llm
        assert services.coding._judge_llm is services.analyze._judge_llm

    @pytest.mark.asyncio
    async def test_default_evaluator_is_a_real_noop_port(self, auth_env: None) -> None:
        """No ``LANGFUSE_*`` configured (the ``auth_env`` default) → a no-op adapter.

        Never ``None``: #354's null-object contract means every judge call
        site can call ``record_judge_scores`` unconditionally.
        """
        services = await build_services(AppSettings(), llm=_StubLLM())

        assert isinstance(services.analyze._evaluator, NoOpEvaluationAdapter)
        assert isinstance(services.summarize._evaluator, NoOpEvaluationAdapter)

    @pytest.mark.asyncio
    async def test_every_service_shares_the_one_evaluator(self, auth_env: None) -> None:
        """One evaluator instance, like the executor and anonymiser above."""
        services = await build_services(AppSettings(), llm=_StubLLM())

        assert services.analyze._evaluator is services.summarize._evaluator

    @pytest.mark.asyncio
    async def test_uses_injected_evaluator(self, auth_env: None) -> None:
        """An ``evaluator=`` override reaches both analyze and summarize."""
        stub_evaluator = _StubEvaluator()

        services = await build_services(
            AppSettings(), llm=_StubLLM(), evaluator=stub_evaluator
        )

        assert services.analyze._evaluator is stub_evaluator
        assert services.summarize._evaluator is stub_evaluator

    @pytest.mark.asyncio
    async def test_uses_injected_prompt_versions(self, auth_env: None) -> None:
        """A ``prompt_versions=`` override reaches every service that needs it (#398).

        Passing one in skips the Langfuse push entirely, which is what lets
        this test run without a real Langfuse instance or a mocked adapter.
        """
        stub_versions = {"analyze-single-pass-system": 3}

        services = await build_services(
            AppSettings(), llm=_StubLLM(), prompt_versions=stub_versions
        )

        assert services.analyze._prompt_versions is stub_versions
        assert services.summarize._prompt_versions is stub_versions
        assert services.coding._prompt_versions is stub_versions
        assert services.sensitivity._prompt_versions is stub_versions
        assert services.prompt_versions is stub_versions


class TestRegisterCustomModelPrices:
    """Judge calls on an unpriced model are silently recorded at zero cost.

    ``completion_cost`` raises for a model missing from litellm's cost map,
    ``LiteLLMClient`` catches that and sets ``cost = float("nan")``, and
    ``_to_decimal`` coerces NaN to ``Decimal("0")`` — so a model absent here
    breaks cost attribution without ever raising. These tests are the
    guard against a candidate judge model being deployed unpriced.
    """

    def test_mistral_medium_3_5_is_priced(self) -> None:
        register_custom_model_prices()

        entry = litellm.model_cost["azure_ai/mistral-medium-3-5"]

        assert entry["input_cost_per_token"] > 0
        assert entry["output_cost_per_token"] > 0


@pytest.fixture
def no_ambient_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``LANGFUSE_*`` so a developer's local ``.env`` can't leak in.

    Same rationale as the ``EMBEDDING_*``/``JUDGE_LLM_*`` clearing in
    ``auth_env`` above: these tests construct a bare or partially-bare
    ``LangfuseSettings()`` and need the unset-fields case to be
    deterministic regardless of ambient environment.
    """
    for var in LANGFUSE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestBuildLangfuseTracer:
    """Langfuse tracing is opt-in and never carries prompt/completion text.

    ``build_langfuse_tracer`` returns a usable ``Tracer`` either way — these
    tests check what it did to get there (attached an OTLP exporter, or
    not), not the object's type.
    """

    def test_does_not_build_an_exporter_without_credentials(
        self, no_ambient_langfuse_env: None
    ) -> None:
        """Local dev has no Langfuse keys, and must not pay for an exporter."""
        with patch("qfa.api.composition.OTLPSpanExporter") as mock_exporter:
            build_langfuse_tracer(LangfuseSettings())

        mock_exporter.assert_not_called()

    def test_configures_the_otlp_exporter_when_credentials_are_set(self) -> None:
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example",
        )

        with patch("qfa.api.composition.OTLPSpanExporter") as mock_exporter:
            build_langfuse_tracer(settings)

        mock_exporter.assert_called_once()
        call_kwargs = mock_exporter.call_args.kwargs
        assert (
            call_kwargs["endpoint"]
            == "https://langfuse.internal.example/api/public/otel/v1/traces"
        )
        expected_auth = base64.b64encode(b"pk-test:sk-test").decode()
        assert call_kwargs["headers"]["Authorization"] == f"Basic {expected_auth}"
        assert call_kwargs["headers"]["x-langfuse-ingestion-version"] == "4"

    def test_strips_a_trailing_slash_from_the_host(self) -> None:
        """A trailing slash in LANGFUSE_HOST must not produce a double slash."""
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example/",
        )

        with patch("qfa.api.composition.OTLPSpanExporter") as mock_exporter:
            build_langfuse_tracer(settings)

        assert (
            mock_exporter.call_args.kwargs["endpoint"]
            == "https://langfuse.internal.example/api/public/otel/v1/traces"
        )

    def test_requires_host_when_credentials_are_set(
        self, no_ambient_langfuse_env: None
    ) -> None:
        """A self-hosted deployment must never fall back to Langfuse Cloud."""
        with pytest.raises(ValidationError, match="LANGFUSE_HOST"):
            LangfuseSettings(public_key="pk-test", secret_key=SecretStr("sk-test"))


class TestBuildEvaluator:
    """Live judge-score delivery is opt-in, mirroring ``build_langfuse_tracer`` (#354).

    ``build_evaluator`` always returns a real ``EvaluationPort`` — a
    no-op one is not "unconfigured", it is the deliberate default.
    """

    def test_returns_a_noop_adapter_without_credentials(
        self, no_ambient_langfuse_env: None
    ) -> None:
        """Local dev has no Langfuse keys, and scores are simply dropped."""
        evaluator = build_evaluator(LangfuseSettings())

        assert isinstance(evaluator, NoOpEvaluationAdapter)

    def test_returns_a_langfuse_adapter_when_credentials_are_set(self) -> None:
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example",
        )

        with patch("qfa.api.composition.LangfuseEvaluationAdapter") as mock_adapter:
            evaluator = build_evaluator(settings)

        mock_adapter.assert_called_once_with(settings)
        assert evaluator is mock_adapter.return_value


class TestBuildPromptVersions:
    """Langfuse prompt sync is opt-in, mirroring ``build_evaluator`` (#398).

    ``build_prompt_versions`` always awaits a real ``PromptPort`` — a no-op
    one is not "unconfigured", it is the deliberate default. Covers the gate
    only; ``LangfusePromptAdapter.sync``'s own compare-then-create behaviour
    is covered in ``tests/adapters/test_prompts.py``.
    """

    @pytest.mark.asyncio
    async def test_routes_to_the_noop_adapter_without_credentials(
        self, no_ambient_langfuse_env: None
    ) -> None:
        """Local dev has no Langfuse keys, and no prompt is ever pushed."""
        result = await build_prompt_versions(LangfuseSettings())

        assert result == {}

    @pytest.mark.asyncio
    async def test_routes_to_the_langfuse_adapter_when_credentials_are_set(
        self,
    ) -> None:
        settings = LangfuseSettings(
            public_key="pk-test",
            secret_key=SecretStr("sk-test"),
            host="https://langfuse.internal.example",
        )

        with patch("qfa.api.composition.LangfusePromptAdapter") as mock_adapter:
            mock_adapter.return_value.sync = AsyncMock(
                return_value={"analyze-judge": 1}
            )
            result = await build_prompt_versions(settings)

        mock_adapter.assert_called_once_with(settings)
        assert result == {"analyze-judge": 1}
