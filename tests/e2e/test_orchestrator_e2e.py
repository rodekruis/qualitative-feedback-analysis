"""Tier-3 end-to-end tests exercising orchestrator → TrackingLLMAdapter → DB.

The LLM port is a ``FakeLLMPort`` injected via ``create_app(llm_factory=...)``;
the lifespan still wraps it in ``TrackingLLMAdapter`` exactly as it would the
real client. Each test queues responses, calls a public route, then queries
the DB directly to verify the recorded row(s).

LiteLLM-specific concerns (cost extraction, exception mapping) are covered
in ``tests/services/test_llm_client.py``; this suite is about wiring.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from qfa.adapters.db import llm_calls
from qfa.domain.errors import LLMError
from qfa.domain.models import LLMResponse
from tests.e2e.conftest import E2E_API_KEY

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def _fetch_rows(e2e_engine):
    async with e2e_engine.connect() as conn:
        rows = (
            await conn.execute(sa.select(llm_calls).order_by(llm_calls.c.id.asc()))
        ).all()
    return rows


def _ok(text: str = "ok", cost: float = 0.0001) -> LLMResponse:
    return LLMResponse(
        text=text,
        model="gpt-3.5-turbo",
        prompt_tokens=5,
        completion_tokens=2,
        cost=cost,
    )


class TestAnalyzeRecordsRow:
    async def test_post_analyze_records_one_ok_row_with_operation_analyze(
        self, e2e_client, e2e_fake_llm, e2e_engine
    ):
        e2e_fake_llm.queue_response(_ok(text="analysis ok"))

        resp = await e2e_client.post(
            "/v1/analyze",
            json={
                "documents": [{"id": "d1", "text": "hello"}],
                "prompt": "summarize",
                "deactivate_anonymization": True,
            },
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200

        rows = await _fetch_rows(e2e_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row.operation == "analyze"
        assert row.status == "ok"
        assert row.error_class is None
        assert row.input_tokens == 5
        assert row.output_tokens == 2


class TestAnalyzeFailureRecordsRow:
    async def test_llm_failure_records_error_row(
        self, e2e_client, e2e_fake_llm, e2e_engine
    ):
        e2e_fake_llm.queue_failure(LLMError("boom"))

        resp = await e2e_client.post(
            "/v1/analyze",
            json={
                "documents": [{"id": "d1", "text": "hello"}],
                "prompt": "summarize",
                "deactivate_anonymization": True,
            },
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        # The orchestrator converts LLMError → AnalysisError → 502.
        assert resp.status_code == 502

        rows = await _fetch_rows(e2e_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row.operation == "analyze"
        assert row.status == "error"
        assert row.error_class == "LLMError"
        assert row.input_tokens == 0
        assert row.output_tokens == 0


class TestAssignCodesRecordsMultipleRows:
    async def test_one_request_records_multiple_rows_all_with_assign_codes(
        self, e2e_client, e2e_fake_llm, e2e_engine
    ):
        # The orchestrator picks Types → Categories → Codes one level at a time.
        # With one type/category/code path it issues 3 LLM calls.
        coding_framework = {
            "types": [
                {
                    "name": "Type A",
                    "categories": [
                        {
                            "name": "Cat A1",
                            "codes": [{"code_id": "code-1", "name": "Code A1.1"}],
                        }
                    ],
                }
            ]
        }
        # The orchestrator parses the response as a JSON list of selected indices.
        # Returning "[0]" each time picks index 0 at every level.
        for _ in range(3):
            e2e_fake_llm.queue_response(_ok(text="[0]"))

        resp = await e2e_client.post(
            "/v1/assign_codes",
            json={
                "coding_framework": coding_framework,
                "feedback_items": [{"id": "f1", "content": "some feedback text"}],
                "max_codes": 5,
            },
            headers={"Authorization": f"Bearer {E2E_API_KEY}"},
        )
        assert resp.status_code == 200

        rows = await _fetch_rows(e2e_engine)
        assert len(rows) == 3
        for row in rows:
            assert row.operation == "assign_codes"
            assert row.status == "ok"
