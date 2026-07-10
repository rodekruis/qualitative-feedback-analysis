"""Tests for API route handlers."""

import httpx
import pytest

from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    FeedbackTooLargeError,
)
from qfa.domain.models import FeedbackRecordSummaryModel

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


def _summary_metadata(**overrides):
    base = {
        "created": "2024-01-15T10:00:00+00:00",
        "coding_level_1": "l1",
        "coding_level_2": "l2",
        "coding_level_3": "l3",
    }
    base.update(overrides)
    return base


def _valid_summary_body(**overrides):
    body = {
        "feedback_record": {
            "id": "doc-1",
            "content": "Great service!",
        },
    }
    body.update(overrides)
    return body


def _valid_detect_sensitive_body(**overrides):
    body = {
        "feedback_record": {
            "id": "doc-1",
            "content": "A staff member asked for a bribe.",
        },
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
            "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "analysis" in data
        assert "feedback_record_count" in data
        assert "request_id" in data

    @pytest.mark.asyncio
    async def test_accepts_legacy_espocrm_metadata_with_feedback_record_id(
        self, client
    ):
        """Old EspoCRM payloads carrying feedback_record_id in metadata get 200.

        Why: pre-v2.0.1 flowcharts copied the record id into metadata as
        feedback_record_id. The formalized metadata forbids unknown keys, so
        without re-admitting this legacy field the backend would 422 every
        feedback/insight save from an un-upgraded EspoCRM. This locks in the
        backward compatibility that lets the backend deploy independently of
        the flowchart upgrade.
        """
        legacy_record = {
            "id": "fi-001",
            "content": "The water distribution was well organized.",
            "metadata": {
                "created": "2024-06-01T12:00:00Z",
                "coding_level_1": "Water",
                "coding_level_2": "Distribution",
                "coding_level_3": "Waiting times",
                "feedback_record_id": "fi-001",
            },
        }
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(feedback_records=[legacy_record]),
            headers=_auth_header(),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_feedback_record_count_matches_input(self, client):
        docs = [
            {"id": "1", "content": "Doc one"},
            {"id": "2", "content": "Doc two"},
            {"id": "3", "content": "Doc three"},
        ]
        resp = await client.post(
            "/v1/analyze-bulk",
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
            "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
        )
        # Raises ValueError if the string isn't a valid UUID.
        UUID(resp.json()["request_id"])

    @pytest.mark.asyncio
    async def test_x_request_id_header_present(self, client):
        resp = await client.post(
            "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
        )
        assert "x-request-id" in resp.headers

    @pytest.mark.asyncio
    async def test_x_request_id_matches_body(self, client):
        resp = await client.post(
            "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
            async def analyze_bulk(self, request, deadline):
                ctx = current_call_context.get()
                assert ctx is not None
                captured["call_id"] = ctx.call_id
                captured["operation"] = ctx.operation
                return AnalysisResultModel(result="ok")

        test_app.state.orchestrator = CapturingOrchestrator()
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
        assert "id" in data
        assert "title" in data
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_response_contains_summary_items(self, client):
        resp = await client.post(
            "/v1/summarize", json=_valid_summary_body(), headers=_auth_header()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "doc-1"
        assert "title" in data
        assert "summary" in data
        assert data["quality_score"] == 0.9

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


class TestSummarizeBulkSuccess:
    @pytest.mark.asyncio
    async def test_localizes_pretty_output_headers(self, client):
        """output_language localizes bulk headers without leaking the field.

        The request's output_language is threaded into the response renderer so
        QUALITY/TITLE/SUMMARY come back translated, the technical IDs label is
        not, and output_language stays out of the serialized body.
        """
        body = {
            "feedback_records": [
                {"id": "doc-1", "content": "Great service!", "metadata": {}}
            ],
            "output_language": "French",
        }
        resp = await client.post(
            "/v1/summarize-bulk", json=body, headers=_auth_header()
        )
        assert resp.status_code == 200
        data = resp.json()
        pretty = data["pretty_output"]
        assert "QUALITÉ" in pretty
        assert "QUALITY:" not in pretty
        # Technical label is not localized.
        assert "IDs:" in pretty
        # Presentation-only field is excluded from the response.
        assert "output_language" not in data


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
        assert "id" in data
        assert "is_sensitive" in data

    @pytest.mark.asyncio
    async def test_response_contains_rating_fields(self, client):
        resp = await client.post(
            "/v1/detect-sensitive",
            json=_valid_detect_sensitive_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "doc-1"
        assert data["is_sensitive"] is True
        assert data["explanation"] == "Contains a bribery allegation."
        assert data["sensitivity_types"] == ["CORRUPTION"]

    @pytest.mark.asyncio
    async def test_detect_sensitive_forwards_metadata(self, test_app):
        body = _valid_detect_sensitive_body(
            feedback_record={
                "id": "doc-1",
                "content": "A staff member asked for a bribe.",
                "metadata": {"coding_level_1": "North"},
            }
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
            test_app.state.orchestrator.last_detect_sensitive_request.feedback_record
        )
        assert record.metadata.coding_level_1 == "North"


# ------------------------------------------------------------------ #
# Authentication
# ------------------------------------------------------------------ #


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_401_missing_authorization_header(self, client):
        resp = await client.post("/v1/analyze-bulk", json=_valid_body())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_401_invalid_api_key(self, client):
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(),
            headers=_auth_header("wrong-key"),
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_401_malformed_authorization(self, client):
        resp = await client.post(
            "/v1/analyze-bulk",
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
            "/v1/analyze-bulk",
            json=_valid_body(feedback_records=[]),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_422_missing_prompt(self, client):
        resp = await client.post(
            "/v1/analyze-bulk",
            json={"feedback_records": [{"id": "1", "text": "data"}]},
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_422_prompt_too_long(self, client):
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(prompt="x" * 4001),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_summary_422_missing_feedback_record(self, client):
        resp = await client.post(
            "/v1/summarize",
            json={},
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_detect_sensitive_422_missing_feedback_record(self, client):
        resp = await client.post(
            "/v1/detect-sensitive",
            json={},
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_422_unknown_metadata_field(self, client):
        """Only created/coding_level_1/2/3 are accepted in metadata.

        Why: ApiFeedbackRecordMetadata used to allow (and silently drop
        via the domain model) any metadata key; it now rejects unknown
        keys outright so a typo'd or corpus-only field (e.g. region)
        fails loudly with a 422 instead of vanishing.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(
                feedback_records=[
                    {
                        "id": "doc-1",
                        "content": "Great service!",
                        "metadata": {"region": "Eastern Province"},
                    }
                ]
            ),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


# ------------------------------------------------------------------ #
# Empty feedback content (issue #138)
# ------------------------------------------------------------------ #


class TestEmptyFeedbackContent:
    """Records with empty ``content`` must not cause silent failures.

    EspoCRM submits feedback records whose description may be blank. The
    API previously rejected the *entire* request with a 422 that EspoCRM
    swallowed, so a single blank record silently broke a whole batch
    (issue #138). Empty records are now accepted at the API boundary and
    dropped before the domain layer (which keeps its non-empty ``content``
    invariant); when nothing is left to process the endpoint returns a 200
    empty result instead of erroring.
    """

    @pytest.mark.asyncio
    async def test_analyze_bulk_drops_empty_records_and_processes_rest(self, client):
        """Mixed batch is accepted (200) and only non-empty records counted.

        Reproduces #138: the blank record previously 422'd the whole batch.
        It is now dropped and the remaining two records are analyzed.
        """
        docs = [
            {"id": "1", "content": "Real feedback one"},
            {"id": "2", "content": ""},
            {"id": "3", "content": "Real feedback three"},
        ]
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(feedback_records=docs),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["feedback_record_count"] == 2

    @pytest.mark.asyncio
    async def test_analyze_bulk_all_empty_returns_empty_result(self, client):
        """All-blank batch returns 200 with a zero-record, disclaimer-only analysis.

        The domain request (which requires >=1 record) is never built; the
        route short-circuits to an empty analysis without an LLM call.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(feedback_records=[{"id": "1", "content": ""}]),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["feedback_record_count"] == 0
        assert body["analysis"].startswith(
            "Disclaimer: Generated by AI. Human review required."
        )

    @pytest.mark.asyncio
    async def test_summarize_bulk_drops_empty_records_and_processes_rest(self, client):
        """summarize-bulk aggregates only non-empty records in a mixed batch.

        The dropped record's id must not appear in the aggregate ``ids``,
        confirming it never reached the orchestrator.
        """
        records = [
            {
                "id": "keep-1",
                "content": "Real feedback",
                "metadata": _summary_metadata(),
            },
            {"id": "drop-1", "content": "", "metadata": _summary_metadata()},
        ]
        resp = await client.post(
            "/v1/summarize-bulk",
            json={"feedback_records": records},
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["ids"] == ["keep-1"]

    @pytest.mark.asyncio
    async def test_summarize_bulk_all_empty_returns_empty_result(self, client):
        """All-blank summarize-bulk batch returns 200 with an empty aggregate."""
        records = [
            {"id": "1", "content": "", "metadata": _summary_metadata()},
        ]
        resp = await client.post(
            "/v1/summarize-bulk",
            json={"feedback_records": records},
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ids"] == []
        assert body["title"] == ""
        assert body["summary"] == ""

    @pytest.mark.asyncio
    async def test_summarize_empty_content_returns_empty_result(self, client):
        """Single /summarize with blank content returns 200 with an empty summary.

        Reproduces #138 for the single-record endpoint: it previously 422'd.
        There is nothing to drop, so the route returns an empty summary that
        still carries the source id, without calling the LLM.
        """
        resp = await client.post(
            "/v1/summarize",
            json=_valid_summary_body(feedback_record={"id": "doc-1", "content": ""}),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "doc-1"
        assert body["title"] == ""
        assert body["summary"] == ""

    @pytest.mark.asyncio
    async def test_assign_codes_empty_content_returns_no_codes(self, client):
        """Single /assign-codes with blank content returns 200 with no codes."""
        body = {
            "feedback_record": {"id": "doc-1", "content": ""},
            "coding_levels": {
                "root_codes": [
                    {
                        "name": "Level 1",
                        "id": "level-1",
                        "children": [
                            {
                                "name": "Level 2",
                                "id": "level-2",
                                "children": [
                                    {"name": "Level 3", "id": "level-3", "children": []}
                                ],
                            }
                        ],
                    }
                ]
            },
        }
        resp = await client.post("/v1/assign-codes", json=body, headers=_auth_header())
        assert resp.status_code == 200
        assert resp.json()["assigned_codes"] == []

    @pytest.mark.asyncio
    async def test_detect_sensitive_empty_content_returns_not_sensitive(self, client):
        """Single /detect-sensitive with blank content returns 200, not sensitive."""
        resp = await client.post(
            "/v1/detect-sensitive",
            json=_valid_detect_sensitive_body(
                feedback_record={"id": "doc-1", "content": ""}
            ),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "doc-1"
        assert body["is_sensitive"] is False
        assert body["sensitivity_types"] == []


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
                "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
                "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
                "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
                "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
                "/v1/analyze-bulk", json=_valid_body(), headers=_auth_header()
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
            summarize_result=FeedbackRecordSummaryModel(
                id="custom-1",
                title="Custom title",
                summary="- Custom point",
                quality_score=0.75,
            )
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/summarize",
                json={"feedback_record": {"id": "custom-1", "content": "Input text"}},
                headers=_auth_header(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "custom-1"
        assert data["title"] == "Custom title"
        assert data["summary"] == "- Custom point"
        assert data["quality_score"] == 0.75

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
    "feedback_record": {"id": "custom-1", "content": "Long waiting times"},
    "coding_levels": {
        "root_codes": [
            {
                "id": "type-1",
                "name": "Type A",
                "children": [
                    {
                        "id": "cat-1",
                        "name": "Category A",
                        "children": [
                            {"id": "code-1", "name": "Code A", "children": []}
                        ],
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
        code_item = resp.json()["assigned_codes"][0]
        assert code_item["confidence_level_1"] == 0.9
        assert code_item["confidence_level_2"] == 0.85
        assert code_item["confidence_level_3"] == 0.8
        assert code_item["confidence_aggregate"] == 0.8
        assert "explanation" in code_item

    @pytest.mark.asyncio
    async def test_response_includes_all_coding_levels(self, client):
        resp = await client.post(
            "/v1/assign-codes", json=_CODING_BODY, headers=_auth_header()
        )
        assert resp.status_code == 200
        code_item = resp.json()["assigned_codes"][0]
        assert code_item["coding_level_1_id"] == "type-1"
        assert code_item["coding_level_1_name"] == "Test Level 1"
        assert code_item["coding_level_2_id"] == "cat-1"
        assert code_item["coding_level_2_name"] == "Test Level 2"
        assert code_item["coding_level_3_id"] == "code-1"
        assert code_item["coding_level_3_name"] == "Test Level 3"

    @pytest.mark.asyncio
    async def test_422_on_invalid_confidence_threshold(self, client):
        resp = await client.post(
            "/v1/assign-codes",
            json={**_CODING_BODY, "confidence_threshold": 1.5},
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
