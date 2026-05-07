"""Tests for settings composition."""

import json

import pytest
from pydantic import ValidationError

from qfa.settings import (
    AppSettings,
    AuthSettings,
    LLMSettings,
    OrchestratorSettings,
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
        assert settings.model == "azure_ai/mistral-medium-2505"

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
        monkeypatch.setenv("LLM_MODEL", "azure_ai/mistral-large-2411")
        monkeypatch.setenv(
            "LLM_API_BASE",
            "https://mistral-large.eastus2.inference.ai.azure.com/",
        )
        settings = LLMSettings()
        assert settings.model == "azure_ai/mistral-large-2411"
        assert (
            settings.api_base == "https://mistral-large.eastus2.inference.ai.azure.com/"
        )

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
        assert settings.api_keys[0].key.get_secret_value() == "sk-abc123"
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
        assert len(settings.auth.api_keys) == 1
        assert settings.auth.api_keys[0].tenant_id == "tenant-1"
        assert settings.orchestrator.chars_per_token == 4
        assert settings.log.loglevel == 10  # DEBUG

    def test_sub_settings_pick_up_env_overrides(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "gpt-3.5-turbo")
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
        assert settings.orchestrator.chars_per_token == 8
