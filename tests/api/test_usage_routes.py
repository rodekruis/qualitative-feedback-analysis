"""Tests for usage tracking API endpoints."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from qfa.domain.models import (
    DistributionStats,
    Operation,
    OperationStats,
    TenantApiKey,
    TokenStats,
    UsageStats,
)

FAKE_API_KEY = "test-key-abc123"
FAKE_SUPERUSER_KEY = "superuser-key-xyz789"

pytestmark = pytest.mark.asyncio


def _make_usage_stats(tenant_id: str | None = "tenant-test", total_calls: int = 5):
    return UsageStats(
        tenant_id=tenant_id,
        total_calls=total_calls,
        failed_calls=1,
        total_cost_usd=Decimal("0.500000"),
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
        by_operation=(
            OperationStats(
                operation=Operation.ANALYZE,
                total_calls=4,
                failed_calls=1,
                cost_usd=Decimal("0.4"),
                input_tokens_total=2000,
                output_tokens_total=800,
            ),
            OperationStats(
                operation=Operation.SUMMARIZE,
                total_calls=1,
                failed_calls=0,
                cost_usd=Decimal("0.1"),
                input_tokens_total=500,
                output_tokens_total=200,
            ),
        ),
    )


class FakeUsageRepository:
    def __init__(self, stats=None, all_stats=None):
        self._stats = stats
        self._all_stats = all_stats or []
        self.last_args: tuple = ()

    async def record_call(self, record):
        pass

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        self.last_args = (tenant_id, from_, to)
        return self._stats

    async def get_all_usage_stats(self, from_=None, to=None):
        self.last_args = (from_, to)
        return self._all_stats


class TestUsageDisabled:
    async def test_usage_503_when_disabled(self, client):
        resp = await client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_tracking_disabled"

    async def test_usage_all_503_when_disabled(self, test_app, client):
        test_app.state.api_keys.append(
            TenantApiKey(
                name="superuser",
                key=FAKE_SUPERUSER_KEY,
                tenant_id="admin",
                is_superuser=True,
                key_id="admin-0",
            )
        )
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_tracking_disabled"


class TestUsageEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        repo = FakeUsageRepository(stats=_make_usage_stats())
        test_app.state.usage_repo = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c, repo

    async def test_returns_200_with_new_shape(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 5
        assert data["failed_calls"] == 1
        assert data["total_cost_usd"] == 0.5
        assert data["tenant_id"] == "tenant-test"
        assert len(data["by_operation"]) == 2
        assert data["by_operation"][0]["operation"] == "analyze"

    async def test_passes_time_filter_to_repo(self, client_with_repo):
        client, repo = client_with_repo
        resp = await client.get(
            "/v1/usage",
            params={"from": "2026-04-01T00:00:00Z", "to": "2026-05-01T00:00:00Z"},
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 200
        _, from_, to = repo.last_args
        assert from_ == datetime(2026, 4, 1, tzinfo=UTC)
        assert to == datetime(2026, 5, 1, tzinfo=UTC)

    async def test_rejects_naive_datetime(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            params={"from": "2026-04-01T00:00:00"},
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 422

    async def test_rejects_to_not_strictly_greater_than_from(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            params={
                "from": "2026-05-01T00:00:00Z",
                "to": "2026-05-01T00:00:00Z",
            },
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 422

    async def test_empty_window_returns_200_zeros(self, test_app):
        test_app.state.usage_repo = FakeUsageRepository(stats=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/v1/usage",
                headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 0
        assert data["failed_calls"] == 0
        assert data["total_cost_usd"] == 0
        assert data["by_operation"] == []

    async def test_requires_authentication(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get("/v1/usage")
        assert resp.status_code == 401


class TestUsageAllEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        test_app.state.api_keys.append(
            TenantApiKey(
                name="superuser",
                key=FAKE_SUPERUSER_KEY,
                tenant_id="admin",
                is_superuser=True,
                key_id="admin-0",
            )
        )
        all_stats = [
            _make_usage_stats(tenant_id="t1", total_calls=3),
            _make_usage_stats(tenant_id="t2", total_calls=7),
            _make_usage_stats(tenant_id=None, total_calls=10),
        ]
        repo = FakeUsageRepository(all_stats=all_stats)
        test_app.state.usage_repo = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c, repo

    async def test_200_for_superuser_with_total_row(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tenants"]) == 2
        assert data["total"]["total_calls"] == 10
        assert data["total"]["tenant_id"] is None

    async def test_403_for_non_superuser(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 403
