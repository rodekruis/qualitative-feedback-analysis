"""Tests for settings composition."""

import json

import pytest
from pydantic import ValidationError

from qfa.settings import (
    AppSettings,
    AuthSettings,
    DatabaseSettings,
    LLMProvider,
    LLMSettings,
    OrchestratorSettings,
)


class TestLLMProvider:
    def test_openai_value(self):
        assert LLMProvider.OPENAI == "openai"

    def test_azure_openai_value(self):
        assert LLMProvider.AZURE_OPENAI == "azure_openai"

    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError):
            LLMProvider("invalid_provider")


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
        settings = LLMSettings()
        assert settings.model == "gpt-4.1-mini"

    def test_default_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.provider == LLMProvider.OPENAI

    def test_default_timeout_seconds(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.timeout_seconds == 115.0

    def test_default_max_retries(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.max_retries == 3

    def test_default_max_total_tokens(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.max_total_tokens == 100_000

    def test_default_azure_endpoint(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.azure_endpoint == ""

    def test_default_api_version(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        settings = LLMSettings()
        assert settings.api_version == ""

    def test_api_key_is_secret(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-super-secret")
        settings = LLMSettings()
        assert "sk-super-secret" not in repr(settings)
        assert "sk-super-secret" not in str(settings)

    def test_override_provider_to_azure(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
        monkeypatch.setenv("LLM_AZURE_ENDPOINT", "https://example.openai.azure.com")
        monkeypatch.setenv("LLM_API_VERSION", "2025-01-01-preview")
        settings = LLMSettings()
        assert settings.provider == LLMProvider.AZURE_OPENAI

    def test_azure_requires_endpoint(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
        monkeypatch.setenv("LLM_API_VERSION", "2025-01-01-preview")
        with pytest.raises(ValidationError, match="LLM_AZURE_ENDPOINT"):
            LLMSettings()

    def test_azure_requires_api_version(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
        monkeypatch.setenv("LLM_AZURE_ENDPOINT", "https://example.openai.azure.com")
        with pytest.raises(ValidationError, match="LLM_API_VERSION"):
            LLMSettings()


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
    def test_defaults(self):
        settings = DatabaseSettings()
        assert settings.url == ""
        assert settings.track_usage is False

    def test_reads_from_db_prefixed_env_vars(self, monkeypatch):
        monkeypatch.setenv("DB_URL", "postgresql+asyncpg://user:pass@host/db")
        monkeypatch.setenv("DB_TRACK_USAGE", "true")
        settings = DatabaseSettings()
        assert settings.url == "postgresql+asyncpg://user:pass@host/db"
        assert settings.track_usage is True


class TestAuthSettings:
    def test_reads_from_auth_prefixed_env_vars(self, monkeypatch):
        keys_json = json.dumps(
            [{"name": "prod", "key": "sk-abc123", "tenant_id": "tenant-1"}]
        )
        monkeypatch.setenv("AUTH_API_KEYS", keys_json)
        settings = AuthSettings()
        assert len(settings.api_keys) == 1
        assert settings.api_keys[0].name == "prod"
        assert settings.api_keys[0].key == "sk-abc123"
        assert settings.api_keys[0].tenant_id == "tenant-1"

    def test_requires_api_keys(self, monkeypatch):
        monkeypatch.delenv("AUTH_API_KEYS", raising=False)
        with pytest.raises(ValidationError):
            AuthSettings()


class TestAppSettings:
    def test_composes_all_sub_settings(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv(
            "AUTH_API_KEYS",
            json.dumps([{"name": "prod", "key": "sk-abc", "tenant_id": "tenant-1"}]),
        )
        settings = AppSettings()
        assert settings.llm.api_key.get_secret_value() == "sk-test"
        assert len(settings.auth.api_keys) == 1
        assert settings.auth.api_keys[0].tenant_id == "tenant-1"
        assert settings.orchestrator.chars_per_token == 4
        assert settings.log.loglevel == 10  # DEBUG

    def test_sub_settings_pick_up_env_overrides(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "gpt-3.5-turbo")
        monkeypatch.setenv(
            "AUTH_API_KEYS",
            json.dumps([{"name": "prod", "key": "sk-abc", "tenant_id": "tenant-1"}]),
        )
        monkeypatch.setenv("ORCHESTRATOR_CHARS_PER_TOKEN", "8")
        settings = AppSettings()
        assert settings.llm.model == "gpt-3.5-turbo"
        assert settings.orchestrator.chars_per_token == 8
