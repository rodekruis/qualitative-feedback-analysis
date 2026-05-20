"""Tier-3 end-to-end tests for the /v1/usage endpoints.

These boot the real FastAPI stack (lifespan migrations included), seed
rows directly via the repository, and call the endpoints over HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from qfa.adapters.db import create_session_factory
from qfa.adapters.usage_repository import SqlAlchemyUsageRepository
from qfa.domain.models import CallStatus, LLMCallRecord, Operation
from tests.e2e.conftest import E2E_API_KEY, E2E_SUPER_KEY, E2E_TENANT_ID

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def _record(
    *,
    tenant_id: str = E2E_TENANT_ID,
    operation: Operation = Operation.ANALYZE,
    timestamp: datetime | None = None,
    cost_usd: Decimal = Decimal("0.001"),
    input_tokens: int = 10,
    output_tokens: int = 5,
    status: CallStatus = CallStatus.OK,
    error_class: str | None = None,
    call_id: UUID | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        operation=operation,
        call_id=call_id if call_id is not None else uuid4(),
        timestamp=timestamp or datetime.now(UTC),
        call_duration_ms=100,
        model="gpt-3.5-turbo" if status == CallStatus.OK else "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        status=status,
        error_class=error_class,
    )


async def _seed(e2e_engine, records: list[LLMCallRecord]) -> None:
    repo = SqlAlchemyUsageRepository(create_session_factory(e2e_engine))
    for r in records:
        await repo.record_call(r)


class TestUsageHappyPath:
    async def test_returns_aggregated_stats(self, e2e_client, e2e_engine):
        await _seed(
            e2e_engine,
            [
                _record(operation=Operation.ANALYZE, cost_usd=Decimal("0.5")),
                _record(operation=Operation.ANALYZE, cost_usd=Decimal("0.3")),
                _record(operation=Operation.SUMMARIZE, cost_usd=Decimal("0.1")),
            ],
        )
        resp = await e2e_client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 3
        assert data["failed_calls"] == 0
        assert data["total_cost_usd"] == pytest.approx(0.9)


class TestUsageTimeFilter:
    async def test_from_inclusive_to_exclusive(self, e2e_client, e2e_engine):
        anchor = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        await _seed(
            e2e_engine,
            [
                _record(timestamp=anchor),
                _record(timestamp=anchor + timedelta(hours=1)),
                _record(timestamp=anchor + timedelta(hours=2)),
                _record(timestamp=anchor - timedelta(seconds=1)),
            ],
        )
        resp = await e2e_client.get(
            "/v1/usage",
            params={
                "from": anchor.isoformat(),
                "to": (anchor + timedelta(hours=2)).isoformat(),
            },
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 2


class TestUsageEdgeCases:
    async def test_empty_window_returns_zeros(self, e2e_client):
        future = datetime.now(UTC) + timedelta(days=365)
        resp = await e2e_client.get(
            "/v1/usage",
            params={
                "from": future.isoformat(),
                "to": (future + timedelta(days=1)).isoformat(),
            },
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 0

    async def test_naive_datetime_rejected(self, e2e_client):
        resp = await e2e_client.get(
            "/v1/usage",
            params={"from": "2026-04-01T00:00:00"},
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 422

    async def test_to_not_strictly_greater_than_from_rejected(self, e2e_client):
        resp = await e2e_client.get(
            "/v1/usage",
            params={
                "from": "2026-05-01T00:00:00Z",
                "to": "2026-05-01T00:00:00Z",
            },
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 422


class TestUsageAll:
    async def test_403_for_non_superuser(self, e2e_client):
        resp = await e2e_client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 403

    async def test_200_for_superuser_with_total_row(self, e2e_client, e2e_engine):
        await _seed(
            e2e_engine,
            [
                _record(tenant_id="t-a", cost_usd=Decimal("0.2")),
                _record(tenant_id="t-b", cost_usd=Decimal("0.3")),
            ],
        )
        resp = await e2e_client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {E2E_SUPER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [t["tenant_id"] for t in data["tenants"]] == ["t-a", "t-b"]
        assert data["total"]["tenant_id"] is None
        assert data["total"]["total_calls"] == 2
        assert data["total"]["total_cost_usd"] == pytest.approx(0.5)


class TestPerInvocationE2E:
    async def test_assign_codes_fan_out_aggregates_to_one_invocation(
        self, e2e_client, e2e_engine
    ):
        """One ``assign_codes`` API call ⇒ one invocation, ≥1 LLM rows.

        This is the headline behaviour change for issue #91: a multi-LLM-call
        operation collapses to a single per-invocation count, while the
        per-LLM-call view (``llm_call_stats``) preserves the raw row count.
        We seed three rows sharing one ``call_id`` to model the fan-out
        rather than rely on the orchestrator path.
        """
        shared = uuid4()
        await _seed(
            e2e_engine,
            [
                LLMCallRecord(
                    tenant_id=E2E_TENANT_ID,
                    operation=Operation.ASSIGN_CODES,
                    call_id=shared,
                    timestamp=datetime.now(UTC),
                    call_duration_ms=100,
                    model="gpt-3.5-turbo",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.001"),
                    status=CallStatus.OK,
                )
                for _ in range(3)
            ],
        )
        resp = await e2e_client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 1
        assert data["llm_call_stats"]["total_calls"] == 3
        ops = data["operations"]
        assert len(ops) == 1
        assert ops[0]["operation"] == "assign_codes"
        assert ops[0]["total_calls"] == 1
        assert ops[0]["llm_call_stats"]["total_calls"] == 3


class TestOperationsBreakdownE2E:
    async def test_analyze_and_summarize_appear_in_operations(
        self, e2e_client, e2e_engine
    ):
        """``operations`` contains entries for each operation actually called.

        Two POST-equivalent seeds with distinct operations ⇒ two entries
        in ``operations`` sorted by cost desc. Verifies the wire shape
        end-to-end: route handler → schema → JSON.
        """
        await _seed(
            e2e_engine,
            [
                _record(operation=Operation.ANALYZE, cost_usd=Decimal("0.5")),
                _record(operation=Operation.SUMMARIZE, cost_usd=Decimal("0.1")),
            ],
        )
        resp = await e2e_client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        ops = [o["operation"] for o in data["operations"]]
        assert ops == ["analyze", "summarize"]  # cost desc


class TestUsageAllOperationsE2E:
    async def test_grand_total_carries_operations(self, e2e_client, e2e_engine):
        """``/v1/usage/all`` grand total carries an operations breakdown.

        Two tenants overlapping on ``analyze`` ⇒ grand total ``operations``
        rolls up to a single ``analyze`` entry whose ``total_calls`` sums
        across tenants.
        """
        await _seed(
            e2e_engine,
            [
                _record(tenant_id="t-a", operation=Operation.ANALYZE),
                _record(tenant_id="t-b", operation=Operation.ANALYZE),
            ],
        )
        resp = await e2e_client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {E2E_SUPER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        total = data["total"]
        assert total["tenant_id"] is None
        assert [o["operation"] for o in total["operations"]] == ["analyze"]
        assert total["operations"][0]["total_calls"] == 2
