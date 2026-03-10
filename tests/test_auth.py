"""Tests for the auth module."""

from unittest.mock import patch

import pytest

from qfa.auth import validate_api_key
from qfa.domain.errors import AuthenticationError
from qfa.domain.models import TenantApiKey

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
            "qfa.auth.secrets.compare_digest",
            wraps=__import__("secrets").compare_digest,
        ) as mock_compare:
            validate_api_key("sk-prod-abc", api_keys)
            assert mock_compare.call_count == len(api_keys)

    def test_compares_all_keys_even_on_match(self, api_keys):
        """Verify constant-time behaviour: all keys are compared, no short-circuit."""
        with patch(
            "qfa.auth.secrets.compare_digest",
            wraps=__import__("secrets").compare_digest,
        ) as mock_compare:
            # Match is the first key, but all keys must still be compared.
            validate_api_key("sk-prod-abc", api_keys)
            assert mock_compare.call_count == len(api_keys)

    def test_empty_key_list_raises(self):
        with pytest.raises(AuthenticationError):
            validate_api_key("sk-whatever", [])
