"""Tests for POST /v1/analyze-bulk — mode field validation and new response fields.

These tests are focused on the spec-117 additions (mode, quality_score,
uncertainty_explanation).  Pre-existing per-endpoint contract tests
(request_id, feedback_record_count, error handling) live in
test_routes.py::TestAnalyzeSuccess.
"""

import pytest

from qfa.domain.models import AnalysisResultModel

from .conftest import FAKE_API_KEY, FakeService


def _auth_header(key=FAKE_API_KEY):
    return {"Authorization": f"Bearer {key}"}


def _valid_body(**overrides):
    """Return a minimal valid /v1/analyze-bulk request body."""
    body = {
        "feedback_records": [{"id": "r1", "content": "Water access was limited."}],
        "prompt": "What are the main themes?",
    }
    body.update(overrides)
    return body


class TestModeField:
    @pytest.mark.asyncio
    async def test_no_mode_field_defaults_to_single_pass_and_returns_200(self, client):
        """Omitting ``mode`` defaults to ``single_pass`` and the request succeeds.

        Backwards-compatibility guard: existing clients that do not send
        ``mode`` must not break after this change.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_explicit_single_pass_mode_returns_200(self, client):
        """Explicitly sending ``"mode": "single_pass"`` is valid and returns 200.

        Confirms the only currently-supported mode is accepted without error.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(mode="single_pass"),
            headers=_auth_header(),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_hierarchical_mode_returns_200(self, client):
        """Sending ``"mode": "hierarchical"`` is valid and returns 200.

        Why: #124 adds hierarchical as a supported mode; the route must
        dispatch to ``analyze_hierarchical`` without a schema rejection.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(mode="hierarchical"),
            headers=_auth_header(),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unsupported_mode_returns_422(self, client):
        """Sending an unsupported ``mode`` value must return 422.

        Pydantic's ``Literal["single_pass", "hierarchical"]`` enforcement is
        the gate; this confirms the schema is wired into FastAPI validation.
        Only values outside the allowlist are rejected.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(mode="batch"),
            headers=_auth_header(),
        )
        assert resp.status_code == 422


class TestResponseShape:
    @pytest.mark.asyncio
    async def test_quality_score_present_in_response(self, client):
        """``quality_score`` is present in the response on the happy path.

        May be ``null`` when the judge fails, but on the happy path the
        fake service returns a non-null score.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "quality_score" in data
        assert data["quality_score"] == 0.85
        assert data["faithfulness"] == 0.9
        assert data["coverage"] == 0.8
        assert data["clarity"] == 0.7

    @pytest.mark.asyncio
    async def test_component_fields_present_in_response(self, client):
        """``faithfulness``, ``coverage`` and ``clarity`` are on the happy-path body.

        ``quality_score`` is the weighted composite of the three, computed
        in Python, so the components must be present for eval to read them.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["faithfulness"] == 0.9
        assert data["coverage"] == 0.8
        assert data["clarity"] == 0.7
        assert data["quality_score"] == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_hierarchical_component_fields_are_null(self, test_app):
        """Hierarchical responses carry the three components as ``null``.

        Per-chunk aggregation is a follow-up; until then the route must
        not invent component values from the coverage-weighted confidence.
        """
        import httpx

        test_app.state.analyze_service = FakeService(
            analyze_result=AnalysisResultModel(
                result="Some analysis.",
                quality_score=None,
                confidence=0.8,
                components=None,
                uncertainty_explanation="leaf scores aggregated",
            )
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            resp = await c.post(
                "/v1/analyze-bulk",
                json=_valid_body(mode="hierarchical"),
                headers=_auth_header(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["confidence"] == pytest.approx(0.8)
        assert data["faithfulness"] is None
        assert data["coverage"] is None
        assert data["clarity"] is None

    @pytest.mark.asyncio
    async def test_uncertainty_explanation_present_in_response(self, client):
        """``uncertainty_explanation`` is present in the response on the happy path.

        Lets the analyst understand the judge's reasoning for the quality score.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "uncertainty_explanation" in data
        assert isinstance(data["uncertainty_explanation"], str)
        assert len(data["uncertainty_explanation"]) > 0

    @pytest.mark.asyncio
    async def test_quality_score_is_null_when_judge_fails(self, test_app):
        """``quality_score`` is ``null`` when the analyze service signals judge failure.

        Simulates the judge-failure path by injecting an ``AnalysisResultModel``
        with ``quality_score=None`` directly via a custom fake service.
        The route must pass that ``None`` through to the client unchanged.
        """
        import httpx

        from qfa.services.prompts import JUDGE_UNAVAILABLE_EXPLANATION

        test_app.state.analyze_service = FakeService(
            analyze_result=AnalysisResultModel(
                result="Some analysis.",
                quality_score=None,
                uncertainty_explanation=JUDGE_UNAVAILABLE_EXPLANATION,
            )
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            resp = await c.post(
                "/v1/analyze-bulk",
                json=_valid_body(),
                headers=_auth_header(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["quality_score"] is None
        assert data["faithfulness"] is None
        assert data["coverage"] is None
        assert data["clarity"] is None
        assert data["uncertainty_explanation"] == JUDGE_UNAVAILABLE_EXPLANATION


class TestOutputLanguage:
    @pytest.mark.asyncio
    async def test_output_language_forwarded_to_analyze_service(
        self, client, fake_service
    ):
        """``output_language`` in the body reaches the domain ``AnalysisRequestModel``.

        Why: #154 — the analyse-bulk route previously accepted ``output_language``
        but never forwarded it, so the field was silently inert.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(output_language="Dutch"),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert fake_service.last_analyze_request.output_language == "Dutch"

    @pytest.mark.asyncio
    async def test_output_language_defaults_to_none_when_omitted(
        self, client, fake_service
    ):
        """Omitting ``output_language`` forwards ``None`` to the domain request.

        Why: confirms the passthrough does not invent a default and stays
        backwards-compatible with clients that never send the field.
        """
        resp = await client.post(
            "/v1/analyze-bulk",
            json=_valid_body(),
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert fake_service.last_analyze_request.output_language is None
