"""Tests for API route handlers."""

import httpx
import pytest

from feedback_analysis_backend.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    DocumentsTooLargeError,
)

from .conftest import FAKE_API_KEY, FakeOrchestrator


def _auth_header(key=FAKE_API_KEY):
    return {"Authorization": f"Bearer {key}"}


def _valid_body(
    documents=None,
    prompt="Summarize the feedback.",
):
    if documents is None:
        documents = [{"id": "doc-1", "text": "Great service!", "metadata": {}}]
    return {"documents": documents, "prompt": prompt}


def _make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


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
        assert "document_count" in data
        assert "request_id" in data

    @pytest.mark.asyncio
    async def test_document_count_matches_input(self, client):
        docs = [
            {"id": "1", "text": "Doc one"},
            {"id": "2", "text": "Doc two"},
            {"id": "3", "text": "Doc three"},
        ]
        resp = await client.post(
            "/v1/analyze", json=_valid_body(documents=docs), headers=_auth_header()
        )
        assert resp.status_code == 200
        assert resp.json()["document_count"] == 3

    @pytest.mark.asyncio
    async def test_request_id_starts_with_req(self, client):
        resp = await client.post(
            "/v1/analyze", json=_valid_body(), headers=_auth_header()
        )
        assert resp.json()["request_id"].startswith("req_")

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
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"


# ------------------------------------------------------------------ #
# Validation (422)
# ------------------------------------------------------------------ #


class TestValidation:
    @pytest.mark.asyncio
    async def test_422_empty_documents(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(documents=[]),
            headers=_auth_header(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["fields"] is not None

    @pytest.mark.asyncio
    async def test_422_missing_prompt(self, client):
        resp = await client.post(
            "/v1/analyze",
            json={"documents": [{"id": "1", "text": "data"}]},
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
    async def test_422_empty_document_text(self, client):
        resp = await client.post(
            "/v1/analyze",
            json=_valid_body(documents=[{"id": "1", "text": ""}]),
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
    async def test_413_documents_too_large(self, test_app):
        test_app.state.orchestrator = FakeOrchestrator(
            error=DocumentsTooLargeError(
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
        test_app.state.orchestrator = FakeOrchestrator(
            error=AnalysisError("some error")
        )
        async with _make_client(test_app) as c:
            resp = await c.post(
                "/v1/analyze", json=_valid_body(), headers=_auth_header()
            )
        assert resp.json()["error"]["request_id"].startswith("req_")


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
