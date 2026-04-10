"""Shared test fixtures for API tests."""

from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from qfa.api.app import (
    RequestIdMiddleware,
    register_exception_handlers,
)
from qfa.api.routes import router
from qfa.domain.models import (
    AnalysisRequest,
    AnalysisResult,
    CodedFeedbackItem,
    CodingAssignmentRequest,
    CodingAssignmentResult,
    FeedbackItemSummary,
    SummaryRequest,
    SummaryResult,
    TenantApiKey,
)
from qfa.domain.ports import OrchestratorPort

FAKE_API_KEY = "test-key-abc123"
FAKE_TENANT_ID = "tenant-test"
FAKE_API_KEY_NAME = "test-key"


class FakeOrchestrator(OrchestratorPort):
    """Fake orchestrator for testing.

    Returns configurable analyze and summarize results or raises a
    configurable exception.
    """

    def __init__(
        self,
        analyze_result=None,
        summarize_result=None,
        error=None,
    ):
        self._analyze_result = analyze_result or AnalysisResult(
            result="Fake analysis result",
            model="gpt-4-test",
            prompt_tokens=10,
            completion_tokens=20,
        )
        self._summarize_result = summarize_result or SummaryResult(
            feedback_item_summaries=(
                FeedbackItemSummary(
                    id="doc-1",
                    title="Fake summary title",
                    summary="- Fake summary point",
                    quality_score=0.9,
                ),
            )
        )
        self._error = error

    async def analyze(
        self,
        request: AnalysisRequest,
        deadline: datetime,
    ) -> AnalysisResult:
        if self._error is not None:
            raise self._error
        return self._analyze_result

    async def summarize(
        self,
        request: SummaryRequest,
        deadline: datetime,
    ) -> SummaryResult:
        if self._error is not None:
            raise self._error
        return self._summarize_result

    async def assign_codes(
        self,
        request: CodingAssignmentRequest,
        deadline: datetime,
    ) -> CodingAssignmentResult:
        if self._error is not None:
            raise self._error
        return CodingAssignmentResult(
            coded_feedback_items=tuple(
                CodedFeedbackItem(feedback_item_id=item.id, assigned_codes=())
                for item in request.feedback_items
            )
        )


@pytest.fixture
def fake_api_keys():
    return [
        TenantApiKey(
            key_id=f"{FAKE_TENANT_ID}-0",
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
