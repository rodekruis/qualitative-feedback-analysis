"""Tests for the EnvironmentAuthLookupAdapter."""

import pytest

from qfa.adapters.env_auth import EnvironmentAuthLookupAdapter
from qfa.domain.models import TenantApiKey
from qfa.domain.ports import AuthLookupPort

pytestmark = pytest.mark.asyncio

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-beta"

KEY_A1_VALUE = "key-alpha-one-abc"
KEY_A2_VALUE = "key-alpha-two-xyz"
KEY_B1_VALUE = "key-beta-one-def"


@pytest.fixture
def key_a1() -> TenantApiKey:
    return TenantApiKey(
        key_id="a1",
        name="Alpha Key 1",
        key=KEY_A1_VALUE,  # type:ignore [ty:invalid-argument-type]
        hashed_key=None,  # type:ignore [ty:invalid-argument-type]
        tenant_id=TENANT_A,
    )


@pytest.fixture
def key_a2() -> TenantApiKey:
    return TenantApiKey(
        key_id="a2",
        name="Alpha Key 2",
        key=KEY_A2_VALUE,  # type:ignore [ty:invalid-argument-type]
        hashed_key=None,  # type:ignore [ty:invalid-argument-type]
        tenant_id=TENANT_A,
        is_superuser=True,
    )


@pytest.fixture
def key_b1() -> TenantApiKey:
    return TenantApiKey(
        key_id="b1",
        name="Beta Key 1",
        key=KEY_B1_VALUE,  # type:ignore [ty:invalid-argument-type]
        hashed_key=None,  # type:ignore [ty:invalid-argument-type]
        tenant_id=TENANT_B,
    )


@pytest.fixture
def adapter(key_a1, key_a2, key_b1) -> EnvironmentAuthLookupAdapter:
    return EnvironmentAuthLookupAdapter([key_a1, key_a2, key_b1])


# ---------------------------------------------------------------------------
# Port conformance
# ---------------------------------------------------------------------------


class TestPortConformance:
    async def test_explicitly_inherits_from_auth_lookup_port(self):
        assert AuthLookupPort in EnvironmentAuthLookupAdapter.__mro__


# ---------------------------------------------------------------------------
# validate_api_key
# ---------------------------------------------------------------------------


class TestValidateApiKey:
    async def test_valid_key_returns_tenant_api_key(self, adapter, key_a1):
        result = await adapter.validate_api_key(KEY_A1_VALUE)
        assert result == key_a1

    async def test_second_tenant_key_returns_correct_record(self, adapter, key_b1):
        result = await adapter.validate_api_key(KEY_B1_VALUE)
        assert result == key_b1

    async def test_superuser_key_is_returned(self, adapter, key_a2):
        result = await adapter.validate_api_key(KEY_A2_VALUE)
        assert result == key_a2
        assert result.is_superuser is True

    async def test_invalid_key_returns_none(self, adapter):
        result = await adapter.validate_api_key("this-is-not-a-real-key")
        assert result is None

    async def test_empty_string_returns_none(self, adapter):
        result = await adapter.validate_api_key("")
        assert result is None

    async def test_empty_key_list_returns_none(self):
        empty_adapter = EnvironmentAuthLookupAdapter([])
        assert await empty_adapter.validate_api_key(KEY_A1_VALUE) is None

    async def test_iterates_all_keys_even_after_match(
        self, monkeypatch, key_a1, key_a2, key_b1
    ):
        """validate_api_key must not short-circuit to avoid timing side-channels."""
        call_count = 0
        original_matches = TenantApiKey.matches_key

        def counting_matches(self, provided_key: str) -> bool:
            nonlocal call_count
            call_count += 1
            return original_matches(self, provided_key)

        monkeypatch.setattr(TenantApiKey, "matches_key", counting_matches)

        adapter = EnvironmentAuthLookupAdapter([key_a1, key_a2, key_b1])
        await adapter.validate_api_key(KEY_A1_VALUE)  # matches the first key

        assert call_count == 3  # all three keys were checked

    async def test_does_not_raise_on_no_match(self, adapter):
        """Port contract: return None instead of raising."""
        result = await adapter.validate_api_key("wrong")
        assert result is None


# ---------------------------------------------------------------------------
# get_auth_keys
# ---------------------------------------------------------------------------


class TestGetAuthKeys:
    async def test_no_filter_returns_all_keys(self, adapter):
        result = await adapter.get_auth_keys()
        assert len(result) == 3

    async def test_tenant_filter_returns_matching_keys(self, adapter):
        result = await adapter.get_auth_keys(tenant_id=TENANT_A)
        assert len(result) == 2
        assert all(r.tenant_id == TENANT_A for r in result)

    async def test_tenant_filter_single_key(self, adapter):
        result = await adapter.get_auth_keys(tenant_id=TENANT_B)
        assert len(result) == 1
        assert result[0].tenant_id == TENANT_B

    async def test_unknown_tenant_returns_empty_list(self, adapter):
        result = await adapter.get_auth_keys(tenant_id="nonexistent-tenant")
        assert result == []

    async def test_hashed_key_excluded_from_result(self, adapter):
        for record in await adapter.get_auth_keys():
            assert "hashed_key" not in record

    async def test_plaintext_key_not_in_result(self, adapter):
        """The 'key' field is already excluded=True on TenantApiKey."""
        for record in await adapter.get_auth_keys():
            assert "key" not in record

    async def test_result_contains_expected_fields(self, adapter, key_a1):
        result = await adapter.get_auth_keys(tenant_id=TENANT_A)
        a1 = next(r for r in result if r.key_id == "a1")
        assert a1.name == key_a1.name
        assert a1.tenant_id == TENANT_A
        assert a1.is_superuser is False
