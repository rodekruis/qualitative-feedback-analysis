"""Tier-3 end-to-end tests exercising orchestrator → TrackingLLMAdapter → DB.

LiteLLM HTTP traffic is intercepted by ``respx`` so the real
``LiteLLMClient`` (including ``response_cost`` extraction) and the real
``TrackingLLMAdapter`` are exercised. Each test calls a public route, then
queries the DB directly to verify the recorded row(s).
"""

from __future__ import annotations

import httpx
import pytest
import respx
import sqlalchemy as sa

from qfa.adapters.db import llm_calls
from tests.e2e.conftest import E2E_API_KEY

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def _fetch_rows(e2e_engine):
    async with e2e_engine.connect() as conn:
        rows = (
            await conn.execute(sa.select(llm_calls).order_by(llm_calls.c.id.asc()))
        ).all()
    return rows


class TestAnalyzeRecordsRow:
    async def test_post_analyze_records_one_ok_row_with_operation_analyze(
        self, e2e_client, e2e_engine, openai_chat_response
    ):
        with respx.mock(base_url="https://api.openai.com") as mock:
            mock.post("/v1/chat/completions").mock(
                return_value=httpx.Response(
                    200, json=openai_chat_response(text="analysis ok")
                )
            )
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
        assert row.input_tokens >= 1
        assert row.output_tokens >= 1


class TestAnalyzeFailureRecordsRow:
    async def test_500_from_provider_records_error_row(self, e2e_client, e2e_engine):
        with respx.mock(base_url="https://api.openai.com") as mock:
            mock.post("/v1/chat/completions").mock(
                return_value=httpx.Response(500, json={"error": {"message": "boom"}})
            )
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
        assert len(rows) >= 1
        last = rows[-1]
        assert last.status == "error"
        assert last.error_class is not None
        assert last.input_tokens == 0
        assert last.output_tokens == 0


class TestAssignCodesRecordsMultipleRows:
    async def test_one_request_records_multiple_rows_all_with_assign_codes(
        self, e2e_client, e2e_engine, openai_chat_response
    ):
        # The orchestrator picks Types → Categories → Codes one level at a time;
        # each level issues one LLM call. With one type/category/code path,
        # we expect 3 LLM calls (and so 3 rows).
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

        with respx.mock(base_url="https://api.openai.com") as mock:
            mock.post("/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=openai_chat_response(text="1"))
            )
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
        assert len(rows) >= 3
        for row in rows:
            assert row.operation == "assign_codes"
            assert row.status == "ok"
