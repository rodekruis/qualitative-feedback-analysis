"""Tests for API route handlers."""

import httpx
import pytest

from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    FeedbackTooLargeError,
)
from qfa.domain.models import FeedbackRecordSummaryModel, SummaryResultModel

from .conftest import FAKE_API_KEY, FakeOrchestrator


def _auth_header(key=FAKE_API_KEY):
    return {"Authorization": f"Bearer {key}"}


def _valid_body(
    feedback_records=None,
    prompt="Summarize the feedback.",
):
    if feedback_records is None:
        feedback_records = [
            {"id": "doc-1", "content": "Great service!", "metadata": {}}
        ]
    return {"feedback_records": feedback_records, "prompt": prompt}


def _make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


def _valid_summary_body(**overrides):
    body = {
        "feedback_records": [
            {
                "id": "doc-1",
                "content": "Great service!",
                "metadata": {"region": "North", "year": 2024},
            },
        ],
    }
    body.update(overrides)
    return body


def _valid_detect_sensitive_body(**overrides):
    body = {
        "feedback_records": [
            {
                "id": "doc-1",
                "content": "A staff member asked for a bribe.",
            },
        ],
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ #
# Success cases
# ------------------------------------------------------------------ #


class TestAnalyzeSuccess:
    @pytest.mark.asyncio
    async def test_200_on_valid_request(self, client):
        resp = await client.post(
            "/v1/analyze", json=_valid_body(), headers=_auth_header()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "analysis" in data
        assert "feedback_record_count" in data
        assert "request_id" in data

    @pytest.mark.asyncio
    async def test_feedback_record_count_matches_input(self, client):
        docs = [
            {"id": "1", "content": "Doc one"},
            {"id": "2", "content": "Doc two"},
            {"id": "3", "content": "Doc three"},
        ]
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(feedback_records=docs),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["feedback_record_count"] == 3

    @pytest.mark.asyncio
    async def test_request_id_is_canonical_uuid(self, client):
        """``request_id`` in the response body is a canonical UUID string.

        The middleware now emits the UUID directly (no ``req_`` prefix) so
        ops/SDKs can cast it straight to a UUID and join against
        ``llm_calls.call_id`` without string manipulation.
        """
        from uuid import UUID

        resp = await client.post(
            "/v1/analyze", json=_valid_body(), headers=_auth_header()
        )
        # Raises ValueError if the string isn't a valid UUID.
        UUID(resp.json()["request_id"])

    @pytest.mark.asyncio
    async def test_x_request_id_header_present(self, client):
        resp = await client.post(
            "/v1/analyze", json=_valid_body(), headers=_auth_header()
        )
        assert "x-request-id" in resp.headers

    @pytest.mark.asyncio
    async def test_x_request_id_matches_body(self, client):
        resp = await client.post(
            "/v1/analyze", json=_valid_body(), headers=_auth_header()
        )
        assert resp.headers["x-request-id"] == resp.json()["request_id"]

    @pytest.mark.asyncio
    async def test_x_request_id_propagates_into_call_scope_as_call_id(self, test_app):
        """The X-Request-ID UUID is the same value seen as ``ctx.call_id``.

        Unit-level proof of the unification across the full middleware →
        dependency → orchestrator chain: the route's
        ``Depends(call_scope_for(Operation.ANALYZE))`` enters call_scope
        before the orchestrator runs, so the orchestrator can read
        ``current_call_context`` directly without entering scope itself.
        Catches a broken middleware/dep/ContextVar wiring at unit-test
        speed, before the e2e tier even runs.
        """
        from uuid import UUID

        from qfa.domain.models import AnalysisResultModel
        from qfa.domain.usage_models import Operation
        from qfa.services.call_context import current_call_context

        captured: dict = {}

        class CapturingOrchestrator:
            async def analyze(self, request, deadline, anonymize=True):
                ctx = current_call_context.get()
                assert ctx is not None
                captured["call_id"] = ctx.call_id
                captured["operation"] = ctx.operation
                return AnalysisResultModel(result="ok")

        test_app.state.orchestrator = CapturingOrchestrator()
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        assert resp.status_code == 200
        header_uuid = UUID(resp.headers["x-request-id"])
        assert captured["call_id"] == header_uuid
        assert captured["operation"] == Operation.ANALYZE


class TestSummarizeSuccess:
    @pytest.mark.asyncio
    async def test_200_on_valid_request(self, client):
        resp = await client.post(
            "/v1/summarize", json=_valid_summary_body(), headers=_auth_header()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "summaries" in data

    @pytest.mark.asyncio
    async def test_response_contains_summary_items(self, client):
        resp = await client.post(
            "/v1/summarize", json=_valid_summary_body(), headers=_auth_header()
        )
        assert resp.status_code == 200
        summary_item = resp.json()["summaries"][0]
        assert summary_item["id"] == "doc-1"
        assert "title" in summary_item
        assert "summary" in summary_item
        assert summary_item["quality_score"] == 0.9

    @pytest.mark.asyncio
    async def test_x_request_id_header_on_summarize(self, client):
        """``X-Request-ID`` is set on summarize responses as a canonical UUID.

        Guards that every endpoint inherits the unified UUID format —
        not just ``/analyze`` which gets the more explicit checks above.
        """
        from uuid import UUID

        resp = await client.post(
            "/v1/summarize", json=_valid_summary_body(), headers=_auth_header()
        )
        assert "x-request-id" in resp.headers
        UUID(resp.headers["x-request-id"])


class TestDetectSensitiveSuccess:
    @pytest.mark.asyncio
    async def test_200_on_valid_request(self, client):
        resp = await client.post(
            "/v1/detect-sensitive",
            json=_valid_detect_sensitive_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "ratings" in data
        assert len(data["ratings"]) == 1

    @pytest.mark.asyncio
    async def test_response_contains_rating_fields(self, client):
        resp = await client.post(
            "/v1/detect-sensitive",
            json=_valid_detect_sensitive_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        rating = resp.json()["ratings"][0]
        assert rating["id"] == "doc-1"
        assert rating["is_sensitive"] is True
        assert rating["explanation"] == "Contains a bribery allegation."
        assert rating["sensitivity_types"] == ["CORRUPTION"]

    @pytest.mark.asyncio
    async def test_detect_sensitive_forwards_metadata(self, test_app):
        body = _valid_detect_sensitive_body(
            feedback_records=[
                {
                    "id": "doc-1",
                    "content": "A staff member asked for a bribe.",
                    "metadata": {"region": "North"},
                }
            ]
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/detect-sensitive",
                json=body,
                headers=_auth_header(),
            )
        assert resp.status_code == 200
        assert test_app.state.orchestrator.last_detect_sensitive_request is not None
        record = (
            test_app.state.orchestrator.last_detect_sensitive_request.feedback_records[
                0
            ]
        )
        assert record.metadata == {"region": "North"}


# ------------------------------------------------------------------ #
# Authentication
# ------------------------------------------------------------------ #


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_401_missing_authorization_header(self, client):
        resp = await client.post("/v1/analyze", json=_valid_body())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_401_invalid_api_key(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(),
            headers=_auth_header("wrong-key"),
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_401_malformed_authorization(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(),
            headers={"Authorization": "Basic xyzverysecrettoken123"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_summary_401_missing_authorization_header(self, client):
        resp = await client.post("/v1/summarize", json=_valid_summary_body())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_detect_sensitive_401_missing_authorization_header(self, client):
        resp = await client.post(
            "/v1/detect-sensitive", json=_valid_detect_sensitive_body()
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"


# ------------------------------------------------------------------ #
# Validation (422)
# ------------------------------------------------------------------ #


class TestValidation:
    @pytest.mark.asyncio
    async def test_422_empty_feedback_records(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(feedback_records=[]),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_422_missing_prompt(self, client):
        resp = await client.post(
            "/v1/analyze",
            json={"feedback_records": [{"id": "1", "content": "data"}]},
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_422_prompt_too_long(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(prompt="x" * 4001),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_422_empty_feedback_record_content(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(feedback_records=[{"id": "1", "content": ""}]),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_summary_422_empty_feedback_records(self, client):
        resp = await client.post(
            "/v1/summarize",
            json=_valid_summary_body(feedback_records=[]),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_summary_422_prompt_too_long(self, client):
        resp = await client.post(
            "/v1/summarize",
            json=_valid_summary_body(prompt="x" * 4001),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_summary_422_empty_feedback_content(self, client):
        resp = await client.post(
            "/v1/summarize",
            json=_valid_summary_body(
                feedback_records=[
                    {"id": "1", "content": "", "metadata": {}},
                ],
            ),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_detect_sensitive_422_empty_feedback_records(self, client):
        resp = await client.post(
            "/v1/detect-sensitive",
            json=_valid_detect_sensitive_body(feedback_records=[]),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None


# ------------------------------------------------------------------ #
# Error mapping
# ------------------------------------------------------------------ #


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_413_feedback_too_large(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=FeedbackTooLargeError(
                "Too large", estimated_tokens=200_000, limit=100_000
            )
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"
        assert "request_id" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_504_analysis_timeout(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisTimeoutError("Deadline exceeded")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "analysis_timeout"
        assert "request_id" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_502_analysis_error(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisError("LLM failure")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "analysis_unavailable"
        assert "request_id" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_500_unexpected_exception(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=RuntimeError("something broke")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "internal_error"
        assert "request_id" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_all_errors_include_request_id(self, test_app):
        """Error envelopes carry the same canonical-UUID ``request_id``.

        A 4xx/5xx must still include a UUID-format ``request_id`` so
        clients can quote it back when reporting issues and ops can grep
        logs / query ``llm_calls`` for the same value.
        """
        from uuid import UUID

        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisError("some error")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        UUID(resp.json()["error"]["request_id"])

    @pytest.mark.asyncio
    async def test_summary_413_feedback_too_large(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=FeedbackTooLargeError(
                "Too large", estimated_tokens=200_000, limit=100_000
            )
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/summarize",
                json=_valid_summary_body(),
                headers=_auth_header(),
            )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"

    @pytest.mark.asyncio
    async def test_summary_504_analysis_timeout(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisTimeoutError("Deadline exceeded")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/summarize",
                json=_valid_summary_body(),
                headers=_auth_header(),
            )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "analysis_timeout"

    @pytest.mark.asyncio
    async def test_summary_502_analysis_error(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisError("LLM failure")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/summarize",
                json=_valid_summary_body(),
                headers=_auth_header(),
            )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "analysis_unavailable"

    @pytest.mark.asyncio
    async def test_summary_500_unexpected_exception(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=RuntimeError("something broke")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/summarize",
                json=_valid_summary_body(),
                headers=_auth_header(),
            )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_summary_returns_configured_result(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            summarize_result=SummaryResultModel(
                feedback_record_summaries=(
                    FeedbackRecordSummaryModel(
                        id="custom-1",
                        title="Custom title",
                        summary="- Custom point",
                        quality_score=0.75,
                    ),
                ),
            )
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/summarize",
                json=_valid_summary_body(
                    feedback_records=[
                        {
                            "id": "custom-1",
                            "content": "Input text",
                            "metadata": {"feedback_record_id": "fi-custom-1"},
                        },
                    ],
                ),
                headers=_auth_header(),
            )
        assert resp.status_code == 200
        summaries = resp.json()["summaries"]
        assert len(summaries) == 1
        s = summaries[0]
        assert s["id"] == "custom-1"
        assert s["title"] == "Custom title"
        assert s["summary"] == "- Custom point"
        assert s["quality_score"] == 0.75
        assert "pretty_output" in s

    @pytest.mark.asyncio
    async def test_detect_sensitive_502_analysis_error(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisError("LLM failure")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/detect-sensitive",
                json=_valid_detect_sensitive_body(),
                headers=_auth_header(),
            )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "analysis_unavailable"


# ------------------------------------------------------------------ #
# Assign-codes endpoint
# ------------------------------------------------------------------ #


_CODING_BODY = {
    "feedback_records": [{"id": "custom-1", "content": "Long waiting times"}],
    "coding_framework": {
        "root_codes": [
            {
                "name": "Type A",
                "children": [
                    {
                        "name": "Category A1",
                        "children": [{"name": "Code A1.1", "children": []}],
                    }
                ],
            }
        ]
    },
}


class TestAssignCodesSuccess:
    @pytest.mark.asyncio
    async def test_response_includes_confidence_fields_and_explanation(self, client):
        resp = await client.post(
            "/v1/assign-codes", json=_CODING_BODY, headers=_auth_header()
        )
        assert resp.status_code == 200
        code_item = resp.json()["coded_feedback_records"][0]["assigned_codes"][0]
        assert code_item["confidence_type"] == 0.9
        assert code_item["confidence_category"] == 0.85
        assert code_item["confidence_code"] == 0.8
        assert code_item["confidence_aggregate"] == 0.8
        assert "explanation" in code_item

    @pytest.mark.asyncio
    async def test_422_on_invalid_confidence_threshold(self, client):
        resp = await client.post(
            "/v1/assign-codes",
            json={**_CODING_BODY, "confidence_threshold": 1.5},
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_422_on_legacy_coding_framework_shape(self, client):
        legacy_shape = {
            **_CODING_BODY,
            "coding_framework": {
                "types": [
                    {
                        "name": "Type A",
                        "categories": [
                            {
                                "name": "Category A1",
                                "codes": [{"code_id": "code-1", "name": "Code A1.1"}],
                            }
                        ],
                    }
                ]
            },
        }
        resp = await client.post(
            "/v1/assign-codes",
            json=legacy_shape,
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


# ------------------------------------------------------------------ #
# Health endpoint
# ------------------------------------------------------------------ #


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200_without_auth(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_response_fields(self, client):
        resp = await client.get("/v1/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
