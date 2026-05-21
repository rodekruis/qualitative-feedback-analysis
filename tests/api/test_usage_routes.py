"""Tests for usage tracking API endpoints."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from qfa.domain.errors import UsageRepositoryUnavailableError
from qfa.domain.models import (
    DistributionStats,
    Operation,
    OperationStats,
    OperationUsageStats,
    TenantStats,
    TokenStats,
    UsageMetrics,
    UsageStats,
)

FAKE_API_KEY = "test-key-abc123"
FAKE_SUPERUSER_KEY = "superuser-key-xyz789"

pytestmark = pytest.mark.asyncio


def _make_metrics(total_calls: int = 5) -> UsageMetrics:
    return UsageMetrics(
        total_calls=total_calls,
        failed_calls=1,
        total_cost_usd=Decimal("0.500000"),
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
    )


def _make_usage_stats(tenant_id: str | None = "tenant-test", total_calls: int = 5):
    op = OperationStats(
        operation=Operation.ANALYZE,
        total_calls=total_calls,
        failed_calls=1,
        total_cost_usd=Decimal("0.500000"),
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
        llm_call_stats=_make_metrics(total_calls=total_calls),
    )
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
        llm_call_stats=_make_metrics(total_calls=total_calls),
        operations=(op,),
    )


def _make_operation_usage_stats(
    operation: Operation | None = Operation.ANALYZE, total_calls: int = 5
) -> OperationUsageStats:
    tenant = TenantStats(
        tenant_id="tenant-test",
        total_calls=total_calls,
        failed_calls=1,
        total_cost_usd=Decimal("0.500000"),
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
        llm_call_stats=_make_metrics(total_calls=total_calls),
    )
    return OperationUsageStats(
        operation=operation,
        total_calls=total_calls,
        failed_calls=1,
        total_cost_usd=Decimal("0.500000"),
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
        llm_call_stats=_make_metrics(total_calls=total_calls),
        tenants=(tenant,),
    )


class FakeUsageRepository:
    def __init__(self, stats=None, all_stats=None, by_operation=None):
        self._stats = stats
        self._all_stats = all_stats or []
        self._by_operation = by_operation or []
        self.last_args: tuple = ()

    async def record_call(self, record):
        pass

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        self.last_args = (tenant_id, from_, to)
        return self._stats

    async def get_all_usage_stats_by_tenant(self, from_=None, to=None):
        self.last_args = (from_, to)
        return self._all_stats

    async def get_all_usage_by_operation(self, from_=None, to=None):
        self.last_args = (from_, to)
        return self._by_operation


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

    async def test_requires_authentication(self, client_with_repo):
        client, _ = client_with_repo
        resp = await client.get("/v1/usage")
        assert resp.status_code == 401

    async def test_response_includes_llm_call_stats_and_operations(
        self, client_with_repo
    ):
        """The /v1/usage response wires ``llm_call_stats`` and ``operations`` through.

        Locks the API contract evolution from issue #91: clients can now
        read a per-LLM-call view and a per-operation breakdown without
        re-aggregating. Failure here means the schemas_usage wrapper
        dropped the inherited fields.
        """
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_call_stats" in data
        assert data["llm_call_stats"]["total_calls"] == 5
        assert isinstance(data["operations"], list)
        assert data["operations"][0]["operation"] == "analyze"
        assert "llm_call_stats" in data["operations"][0]


class TestUsageAllByTenantEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
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
        """``/v1/usage/all/by-tenant`` returns 200 with tenants + grand-total.

        Guards the renamed path (was ``/v1/usage/all``); confirms the
        wire shape still splits per-tenant from the ``tenant_id=null``
        grand-total entry.
        """
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all/by-tenant",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tenants"]) == 2
        assert data["total"]["total_calls"] == 10
        assert data["total"]["tenant_id"] is None

    async def test_403_for_non_superuser(self, client_with_repo):
        """Non-superusers get 403 on the by-tenant endpoint.

        Guards the superuser dependency stays wired to the renamed path.
        """
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all/by-tenant",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 403

    async def test_old_path_v1_usage_all_returns_404(self, client_with_repo):
        """The pre-rename ``/v1/usage/all`` path no longer exists.

        Hard-move (not aliased): pins the contract change so a future
        accidental re-introduction of the old route is caught by a test.
        """
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 404


class TestUsageAllByOperationEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        by_operation = [
            _make_operation_usage_stats(operation=Operation.ANALYZE, total_calls=4),
            _make_operation_usage_stats(operation=Operation.SUMMARIZE, total_calls=6),
            _make_operation_usage_stats(operation=None, total_calls=10),
        ]
        repo = FakeUsageRepository(by_operation=by_operation)
        test_app.state.usage_repo = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c, repo

    async def test_200_for_superuser_with_operations_and_total(self, client_with_repo):
        """``/v1/usage/all/by-operation`` returns 200 with operations + grand-total.

        Locks the wire shape: ``operations`` is a list of operation
        blocks (excluding the grand-total), and ``total`` is the
        ``operation=null`` entry. Each operation block carries its own
        ``tenants`` breakdown plus ``llm_call_stats``.
        """
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all/by-operation",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["operations"]) == 2
        ops = {entry["operation"] for entry in data["operations"]}
        assert ops == {"analyze", "summarize"}
        assert data["total"]["total_calls"] == 10
        assert data["total"]["operation"] is None
        # Each operation block carries its own tenants list + llm_call_stats.
        first = data["operations"][0]
        assert "llm_call_stats" in first
        assert isinstance(first["tenants"], list)
        assert first["tenants"][0]["tenant_id"] == "tenant-test"

    async def test_403_for_non_superuser(self, client_with_repo):
        """Non-superusers get 403 on the by-operation endpoint.

        Mirrors the by-tenant guard; cross-tenant visibility requires
        superuser.
        """
        client, _ = client_with_repo
        resp = await client.get(
            "/v1/usage/all/by-operation",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 403

    async def test_passes_time_filter_to_repo(self, client_with_repo):
        """``from``/``to`` query params reach the repository on by-operation.

        Guards the time-window plumbing; failures here would mean the
        new route forgot to forward the parsed datetimes.
        """
        client, repo = client_with_repo
        resp = await client.get(
            "/v1/usage/all/by-operation",
            params={"from": "2026-04-01T00:00:00Z", "to": "2026-05-01T00:00:00Z"},
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 200
        from_, to = repo.last_args
        assert from_ == datetime(2026, 4, 1, tzinfo=UTC)
        assert to == datetime(2026, 5, 1, tzinfo=UTC)

    async def test_empty_window_returns_zero_total(self, test_app):
        """When the repo returns no rows, the response carries a zero grand total.

        Pins the fallback path (``_zero_operation_usage_stats``) — the
        ``total`` entry must always be present even if the repo returns
        an empty list.
        """
        repo = FakeUsageRepository(by_operation=[])
        test_app.state.usage_repo = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/v1/usage/all/by-operation",
                headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["operations"] == []
        assert data["total"]["total_calls"] == 0
        assert data["total"]["operation"] is None


class _UnavailableUsageRepository:
    """Fake repo whose reads raise ``UsageRepositoryUnavailableError``.

    Models the wired-but-unreachable case.
    """

    async def record_call(self, record):
        pass

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        raise UsageRepositoryUnavailableError("connection refused")

    async def get_all_usage_stats_by_tenant(self, from_=None, to=None):
        raise UsageRepositoryUnavailableError("connection refused")

    async def get_all_usage_by_operation(self, from_=None, to=None):
        raise UsageRepositoryUnavailableError("connection refused")


class TestUsageBackendUnavailable:
    async def test_usage_503_with_backend_unavailable_code(self, test_app):
        """``/v1/usage`` returns 503 with the typed code when the repo is down.

        Guards the domain → HTTP translation for the per-tenant endpoint.
        """
        test_app.state.usage_repo = _UnavailableUsageRepository()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/v1/usage",
                headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
            )
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "usage_backend_unavailable"

    async def test_usage_all_by_tenant_503_with_backend_unavailable_code(
        self, test_app
    ):
        """``/v1/usage/all/by-tenant`` returns 503 with the typed code.

        Confirms the renamed path keeps the backend-unavailable mapping.
        """
        test_app.state.usage_repo = _UnavailableUsageRepository()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/v1/usage/all/by-tenant",
                headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
            )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_backend_unavailable"

    async def test_usage_all_by_operation_503_with_backend_unavailable_code(
        self, test_app
    ):
        """``/v1/usage/all/by-operation`` returns 503 with the typed code.

        Guards the new endpoint surfaces the same error envelope.
        """
        test_app.state.usage_repo = _UnavailableUsageRepository()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/v1/usage/all/by-operation",
                headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
            )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_backend_unavailable"
