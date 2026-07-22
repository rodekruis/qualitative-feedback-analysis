"""Tests for settings composition."""

import json

import pytest
from pydantic import ValidationError

from qfa.settings import (
    AppSettings,
    AuthSettings,
    DatabaseSettings,
    LLMSettings,
    OrchestratorSettings,
    TelemetrySettings,
)


class TestLLMSettings:
    def test_reads_from_llm_prefixed_env_vars(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
        monkeypatch.setenv("LLM_MODEL", "gpt-3.5-turbo")
        settings = LLMSettings()
        assert settings.model == "gpt-3.5-turbo"
        assert settings.api_key.get_secret_value() == "sk-test-key"

    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with pytest.raises(ValidationError):
            LLMSettings()

    def test_default_model(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.delenv("LLM_MODEL", raising=False)
        settings = LLMSettings()
        assert settings.model == "azure_ai/gpt-5.4"

    def test_default_timeout_seconds(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.timeout_seconds == 115.0

    def test_default_max_total_tokens(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.max_total_tokens == 100_000

    def test_default_api_base(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.delenv("LLM_API_BASE", raising=False)
        settings = LLMSettings()
        assert settings.api_base == ""

    def test_default_api_version(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.delenv("LLM_API_VERSION", raising=False)
        settings = LLMSettings()
        assert settings.api_version == ""

    def test_api_key_is_secret(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-super-secret")
        settings = LLMSettings()
        assert "sk-super-secret" not in repr(settings)
        assert "sk-super-secret" not in str(settings)

    def test_azure_ai_model_with_api_base(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "azure_ai/gpt-5.4")
        monkeypatch.setenv(
            "LLM_API_BASE",
            "https://gpt-5.4.eastus2.inference.ai.azure.com/",
        )
        settings = LLMSettings()
        assert settings.model == "azure_ai/gpt-5.4"
        assert settings.api_base == "https://gpt-5.4.eastus2.inference.ai.azure.com/"

    def test_azure_openai_model_with_api_base_and_version(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "azure/my-gpt4-deployment")
        monkeypatch.setenv("LLM_API_BASE", "https://example.openai.azure.com")
        monkeypatch.setenv("LLM_API_VERSION", "2025-01-01-preview")
        settings = LLMSettings()
        assert settings.model == "azure/my-gpt4-deployment"
        assert settings.api_base == "https://example.openai.azure.com"
        assert settings.api_version == "2025-01-01-preview"


class TestOrchestratorSettings:
    def test_reads_from_orchestrator_prefixed_env_vars(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_RETRY_BASE_SECONDS", "2.0")
        settings = OrchestratorSettings()
        assert settings.retry_base_seconds == 2.0

    def test_default_metadata_fields(self):
        settings = OrchestratorSettings()
        assert settings.metadata_fields_to_include == []

    def test_default_retry_base_seconds(self):
        settings = OrchestratorSettings()
        assert settings.retry_base_seconds == 1.0

    def test_default_retry_multiplier(self):
        settings = OrchestratorSettings()
        assert settings.retry_multiplier == 2.0

    def test_default_retry_jitter_factor(self):
        settings = OrchestratorSettings()
        assert settings.retry_jitter_factor == 0.5

    def test_default_retry_cap_seconds(self):
        settings = OrchestratorSettings()
        assert settings.retry_cap_seconds == 10.0

    def test_default_chars_per_token(self):
        settings = OrchestratorSettings()
        assert settings.chars_per_token == 4


class TestDatabaseSettings:
    def test_reads_from_db_prefixed_env_vars(self, monkeypatch):
        monkeypatch.setenv("DB_URL", "postgresql+asyncpg://user:pass@host/db")
        settings = DatabaseSettings()
        assert settings.url == "postgresql+asyncpg://user:pass@host/db"

    def test_accepts_parts_based_password_config(self, monkeypatch):
        monkeypatch.delenv("DB_URL", raising=False)
        monkeypatch.setenv("DB_HOST", "db.internal")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_NAME", "qfa")
        monkeypatch.setenv("DB_USER", "qfaadmin")
        monkeypatch.setenv("DB_PASSWORD", "secret")
        settings = DatabaseSettings()
        assert settings.url == ""
        assert settings.auth_mode == "password"
        assert settings.host == "db.internal"

    def test_requires_password_in_password_mode_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("DB_URL", raising=False)
        monkeypatch.setenv("DB_AUTH_MODE", "password")
        monkeypatch.setenv("DB_HOST", "db.internal")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_NAME", "qfa")
        monkeypatch.setenv("DB_USER", "qfaadmin")
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        with pytest.raises(ValidationError):
            DatabaseSettings()

    def test_accepts_entra_mode_without_password(self, monkeypatch):
        monkeypatch.delenv("DB_URL", raising=False)
        monkeypatch.setenv("DB_AUTH_MODE", "entra")
        monkeypatch.setenv("DB_HOST", "db.internal")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_NAME", "qfa")
        monkeypatch.setenv("DB_USER", "app-msi")
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        settings = DatabaseSettings()
        assert settings.auth_mode == "entra"

    def test_requires_parts_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("DB_URL", raising=False)
        monkeypatch.delenv("DB_HOST", raising=False)
        monkeypatch.delenv("DB_USER", raising=False)
        monkeypatch.delenv("DB_NAME", raising=False)
        with pytest.raises(ValidationError):
            DatabaseSettings()


class TestAuthSettings:
    def test_reads_from_auth_prefixed_env_vars(self, monkeypatch):
        keys_json = json.dumps(
            [
                {
                    "key_id": "tenant-1-0",
                    "name": "prod",
                    "key": "sk-abc123",
                    "tenant_id": "tenant-1",
                }
            ]
        )
        monkeypatch.setenv("AUTH_API_KEYS", keys_json)
        settings = AuthSettings()
        assert len(settings.api_keys) == 1
        assert settings.api_keys[0].name == "prod"
        assert settings.api_keys[0].key is None
        assert settings.api_keys[0].matches_key("sk-abc123") is True
        assert settings.api_keys[0].tenant_id == "tenant-1"

    def test_requires_api_keys(self, monkeypatch):
        monkeypatch.delenv("AUTH_API_KEYS", raising=False)
        with pytest.raises(ValidationError):
            AuthSettings()


def test_embedding_settings_defaults_and_env_prefix(monkeypatch) -> None:
    """``EmbeddingSettings`` reads ``EMBEDDING_*`` env vars with sane defaults.

    Why: the hierarchical path needs the model artifact path, pinned hash,
    and thread count to be configurable per-environment without code change.
    """
    from qfa.settings import EmbeddingSettings

    monkeypatch.setenv("EMBEDDING_MODEL_PATH", "/srv/models/bge-m3/model.onnx")
    monkeypatch.setenv("EMBEDDING_TOKENIZER_PATH", "/srv/models/bge-m3/tokenizer.json")
    monkeypatch.setenv("EMBEDDING_REVISION_HASH", "sha256:abc123")
    monkeypatch.delenv("EMBEDDING_MAX_TOKENS", raising=False)
    settings = EmbeddingSettings()
    assert settings.model_path == "/srv/models/bge-m3/model.onnx"
    assert settings.revision_hash == "sha256:abc123"
    # Default thread count is None (leave onnxruntime core-count default).
    assert settings.intra_op_num_threads is None
    # Family/dimension default to multilingual-e5-base (the default model);
    # max_tokens=None means "use the family's natural context" (512 for e5).
    assert settings.model_kind == "e5"
    assert settings.dense_dim == 768
    assert settings.max_tokens is None


def test_embedding_settings_select_bge_m3_family(monkeypatch) -> None:
    """``EMBEDDING_MODEL_KIND``/``EMBEDDING_DENSE_DIM``/``EMBEDDING_MAX_TOKENS`` are env-configurable.

    Why: the default is the smaller/faster e5-base, but switching to the
    stronger BGE-M3 (1024-d, pre-pooled) must be a pure config change — no code
    edit — so an operator can trade latency for cross-lingual quality per
    deployment.
    """
    from qfa.settings import EmbeddingSettings

    monkeypatch.setenv("EMBEDDING_MODEL_PATH", "/srv/models/bge-m3/model.onnx")
    monkeypatch.setenv("EMBEDDING_REVISION_HASH", "sha256:bge")
    monkeypatch.setenv("EMBEDDING_MODEL_KIND", "bge-m3")
    monkeypatch.setenv("EMBEDDING_DENSE_DIM", "1024")
    settings = EmbeddingSettings()
    assert settings.model_kind == "bge-m3"
    assert settings.dense_dim == 1024


def test_analyze_settings_have_clustering_and_trend_fields() -> None:
    """``AnalyzeSettings`` exposes clustering params, code-field mapping, and period.

    Why: the analyze-endpoint tuning (clustering parameters and the
    code metadata-field mapping) must be tunable without code changes,
    per the design spec. ``AnalyzeSettings`` is the endpoint-scoped
    group so the eventual orchestrator decomposition (ADR-011) doesn't
    require renaming env vars in production. The default period is
    ``week`` — week is usually the right granularity for the typical
    1-3 month operational corpus. The date field itself (``created``)
    is not configurable: it is the only date-shaped field
    ``FeedbackRecordMetadataModel`` declares.
    """
    from qfa.settings import AnalyzeSettings

    settings = AnalyzeSettings()
    assert settings.min_cluster_size >= 2
    assert isinstance(settings.coding_trend_code_fields, list)
    assert settings.default_coding_trend_period == "week"


def test_analyze_settings_default_period_overridable_via_env(monkeypatch) -> None:
    """``ANALYZE_DEFAULT_CODING_TREND_PERIOD`` overrides the server default.

    Why: operators may want to flip the default for a deployment that
    typically ingests multi-year corpora (where ``month`` is the
    sensible bucket) without forcing every caller to pass ``period``
    on every request.
    """
    from qfa.settings import AnalyzeSettings

    monkeypatch.setenv("ANALYZE_DEFAULT_CODING_TREND_PERIOD", "month")
    assert AnalyzeSettings().default_coding_trend_period == "month"


class TestAppSettings:
    def test_composes_all_sub_settings(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("DB_URL", "postgresql+asyncpg://user:pass@host/db")
        monkeypatch.setenv(
            "AUTH_API_KEYS",
            json.dumps(
                [
                    {
                        "key_id": "tenant-1-0",
                        "name": "prod",
                        "key": "sk-abc",
                        "tenant_id": "tenant-1",
                    }
                ]
            ),
        )
        settings = AppSettings()
        assert settings.llm.api_key.get_secret_value() == "sk-test"
        assert settings.db.url == "postgresql+asyncpg://user:pass@host/db"
        assert len(settings.auth.api_keys) == 1
        assert settings.auth.api_keys[0].tenant_id == "tenant-1"
        assert settings.orchestrator.chars_per_token == 4
        assert settings.log.loglevel == 10  # DEBUG

    def test_sub_settings_pick_up_env_overrides(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "gpt-3.5-turbo")
        monkeypatch.setenv("DB_URL", "postgresql+asyncpg://user:pass@host/db")
        monkeypatch.setenv(
            "AUTH_API_KEYS",
            json.dumps(
                [
                    {
                        "key_id": "tenant-1-0",
                        "name": "prod",
                        "key": "sk-abc",
                        "tenant_id": "tenant-1",
                    }
                ]
            ),
        )
        monkeypatch.setenv("ORCHESTRATOR_CHARS_PER_TOKEN", "8")
        settings = AppSettings()
        assert settings.llm.model == "gpt-3.5-turbo"
        assert settings.db.url == "postgresql+asyncpg://user:pass@host/db"
        assert settings.orchestrator.chars_per_token == 8

    def test_composes_telemetry_and_reads_connection_string(self, monkeypatch):
        """The telemetry group is composed in and picks up the env var.

        qfa.main logs the full AppSettings at startup, so the connection string
        must be reachable (and masked — see TestTelemetrySettings) via the
        composed group.
        """
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("DB_URL", "postgresql+asyncpg://user:pass@host/db")
        monkeypatch.setenv("AUTH_API_KEYS", "[]")
        monkeypatch.setenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "InstrumentationKey=00000000-0000-0000-0000-000000000000",
        )
        settings = AppSettings()
        assert settings.telemetry.applicationinsights_connection_string is not None
        assert (
            settings.telemetry.applicationinsights_connection_string.get_secret_value()
            == "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        )


class TestTelemetrySettings:
    def test_connection_string_defaults_to_none(self, monkeypatch):
        """Absent the env var, telemetry is off — the field must default to None.

        qfa.main gates configure_azure_monitor() on this value, so a None
        default is what keeps local dev from exporting telemetry.
        """
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        settings = TelemetrySettings()
        assert settings.applicationinsights_connection_string is None

    def test_constructs_without_other_app_env(self, monkeypatch):
        """TelemetrySettings has no required fields, so a bare import is safe.

        qfa.main builds it at module scope before the rest of the app config
        exists; this guards the docs build (Sphinx imports qfa.main with no env
        set) against regressing back to a ValidationError.
        """
        for var in ("LLM_API_KEY", "DB_URL", "AUTH_API_KEYS"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        # Must not raise despite the LLM/DB/auth env being absent.
        assert TelemetrySettings().applicationinsights_connection_string is None

    def test_connection_string_reads_from_env(self, monkeypatch):
        """The connection string is sourced from the env var, not hardcoded.

        Confirms the App Service app_setting reaches settings so qfa.main can
        pass it to the Azure Monitor SDK rather than reading os.environ directly.
        """
        monkeypatch.setenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "InstrumentationKey=00000000-0000-0000-0000-000000000000",
        )
        settings = TelemetrySettings()
        assert settings.applicationinsights_connection_string is not None
        assert (
            settings.applicationinsights_connection_string.get_secret_value()
            == "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        )

    def test_connection_string_is_masked(self, monkeypatch):
        """The connection string embeds an ingestion key, so it must not leak.

        qfa.main logs the full settings JSON at startup; SecretStr keeps the
        value out of repr/str/model_dump so it never lands in the log stream.
        """
        monkeypatch.setenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "InstrumentationKey=super-secret-key",
        )
        settings = TelemetrySettings()
        assert "super-secret-key" not in settings.model_dump_json()
        assert "super-secret-key" not in str(settings)
