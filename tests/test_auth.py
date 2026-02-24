"""Tests for the auth module."""

import json
from unittest.mock import patch

import pytest

from feedback_analysis_backend.auth import load_api_keys, validate_api_key
from feedback_analysis_backend.domain.errors import AuthenticationError
from feedback_analysis_backend.domain.models import TenantApiKey

# --- load_api_keys ---


class TestLoadApiKeys:
    def _write_config(self, tmp_path, data):
        config = tmp_path / "api_keys.json"
        config.write_text(json.dumps(data), encoding="utf-8")
        return config

    def test_valid_single_key(self, tmp_path):
        config = self._write_config(
            tmp_path,
            [{"name": "prod", "key": "sk-abc123", "tenant_id": "tenant-1"}],
        )
        keys = load_api_keys(config)
        assert len(keys) == 1
        assert isinstance(keys[0], TenantApiKey)
        assert keys[0].name == "prod"
        assert keys[0].key == "sk-abc123"
        assert keys[0].tenant_id == "tenant-1"

    def test_valid_multiple_keys(self, tmp_path):
        config = self._write_config(
            tmp_path,
            [
                {"name": "prod", "key": "sk-abc", "tenant_id": "tenant-1"},
                {"name": "staging", "key": "sk-def", "tenant_id": "tenant-2"},
                {"name": "dev", "key": "sk-ghi", "tenant_id": "tenant-3"},
            ],
        )
        keys = load_api_keys(config)
        assert len(keys) == 3

    def test_file_not_found(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            load_api_keys(missing)

    def test_invalid_json(self, tmp_path):
        config = tmp_path / "api_keys.json"
        config.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_api_keys(config)

    def test_json_not_array(self, tmp_path):
        config = self._write_config(tmp_path, {"name": "prod", "key": "sk-abc"})
        with pytest.raises(ValueError, match="must be a JSON array"):
            load_api_keys(config)

    def test_missing_required_fields(self, tmp_path):
        config = self._write_config(
            tmp_path,
            [{"name": "prod"}],
        )
        with pytest.raises(ValueError, match="Invalid API key entry"):
            load_api_keys(config)

    def test_empty_array(self, tmp_path):
        config = self._write_config(tmp_path, [])
        keys = load_api_keys(config)
        assert keys == []


# --- validate_api_key ---


class TestValidateApiKey:
    @pytest.fixture()
    def api_keys(self):
        return [
            TenantApiKey(name="prod", key="sk-prod-abc", tenant_id="tenant-1"),
            TenantApiKey(name="staging", key="sk-staging-def", tenant_id="tenant-2"),
        ]

    def test_valid_key_matches(self, api_keys):
        result = validate_api_key("sk-prod-abc", api_keys)
        assert result.name == "prod"
        assert result.tenant_id == "tenant-1"

    def test_invalid_key_raises(self, api_keys):
        with pytest.raises(AuthenticationError):
            validate_api_key("sk-invalid", api_keys)

    def test_empty_key_raises(self, api_keys):
        with pytest.raises(AuthenticationError):
            validate_api_key("", api_keys)

    def test_correct_tenant_matched_from_multiple(self, api_keys):
        result = validate_api_key("sk-staging-def", api_keys)
        assert result.name == "staging"
        assert result.tenant_id == "tenant-2"

    def test_uses_secrets_compare_digest(self, api_keys):
        with patch(
            "feedback_analysis_backend.auth.secrets.compare_digest",
            wraps=__import__("secrets").compare_digest,
        ) as mock_compare:
            validate_api_key("sk-prod-abc", api_keys)
            assert mock_compare.call_count == len(api_keys)

    def test_compares_all_keys_even_on_match(self, api_keys):
        """Verify constant-time behaviour: all keys are compared, no short-circuit."""
        with patch(
            "feedback_analysis_backend.auth.secrets.compare_digest",
            wraps=__import__("secrets").compare_digest,
        ) as mock_compare:
            # Match is the first key, but all keys must still be compared.
            validate_api_key("sk-prod-abc", api_keys)
            assert mock_compare.call_count == len(api_keys)

    def test_empty_key_list_raises(self):
        with pytest.raises(AuthenticationError):
            validate_api_key("sk-whatever", [])
