"""Shared test fixtures for API tests."""

from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from feedback_analysis_backend.api.app import (
    RequestIdMiddleware,
    register_exception_handlers,
)
from feedback_analysis_backend.api.routes import router
from feedback_analysis_backend.domain.models import (
    AnalysisRequest,
    AnalysisResult,
    TenantApiKey,
)
from feedback_analysis_backend.domain.ports import OrchestratorPort

FAKE_API_KEY = "test-key-abc123"
FAKE_TENANT_ID = "tenant-test"
FAKE_API_KEY_NAME = "test-key"


class FakeOrchestrator(OrchestratorPort):
    """Fake orchestrator for testing.

    Returns a configurable ``AnalysisResult`` or raises a configurable
    exception.
    """

    def __init__(
        self,
        result=None,
        error=None,
    ):
        self._result = result or AnalysisResult(
            result="Fake analysis result",
            model="gpt-4-test",
            prompt_tokens=10,
            completion_tokens=20,
        )
        self._error = error

    async def analyze(
        self,
        request: AnalysisRequest,
        deadline: datetime,
    ) -> AnalysisResult:
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def fake_api_keys():
    return [
        TenantApiKey(
            name=FAKE_API_KEY_NAME,
            key=FAKE_API_KEY,
            tenant_id=FAKE_TENANT_ID,
        )
    ]


@pytest.fixture
def fake_orchestrator():
    return FakeOrchestrator()


@pytest.fixture
def test_app(fake_orchestrator, fake_api_keys):
    app = FastAPI(title="Test App")
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)
    register_exception_handlers(app)

    app.state.orchestrator = fake_orchestrator
    app.state.api_keys = fake_api_keys

    return app


@pytest_asyncio.fixture
async def client(test_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
    ) as c:
        yield c
