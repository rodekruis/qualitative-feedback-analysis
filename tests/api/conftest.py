"""Shared test fixtures for API tests."""

from datetime import datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from qfa.adapters.env_auth import EnvironmentAuthLookupAdapter
from qfa.api.app import (
    RequestIdMiddleware,
    register_exception_handlers,
)
from qfa.api.routes import router
from qfa.api.routes_admin import router as auth_router
from qfa.api.routes_usage import router as usage_router
from qfa.domain.models import (
    AggregateSummaryResultModel,
    AnalysisRequestModel,
    AnalysisResultModel,
    AssignedCodeModel,
    CodedFeedbackRecordModel,
    CodingAssignmentRequestModel,
    CodingAssignmentResultModel,
    FeedbackRecordSummaryModel,
    KeyCreationResponse,
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityAnalysisResultModelList,
    SummaryRequestModel,
    SummaryResultModel,
    TenantApiKey,
    TenantInfo,
)
from qfa.domain.ports import UsageRepositoryPort
from qfa.domain.sensitivity_types import SensitivityType
from qfa.domain.usage_models import (
    DistributionStats,
    OperationUsageStats,
    UsageMetrics,
    UsageStats,
)
from qfa.services.auth_orchestrator import AuthOrchestrator


class FakeAuthManagementPort:
    """Fake auth-management adapter for API tests."""

    def __init__(self) -> None:
        self._tenant_id_counter = 0

    async def add_tenant(
        self,
        tenant_name: str,
        allows_superusers: bool = False,
    ) -> str:
        self._tenant_id_counter += 1
        return f"tenant-created-{self._tenant_id_counter}"

    async def delete_tenant(self, tenant_id: str) -> None:
        return None

    async def add_key(
        self,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> KeyCreationResponse:
        return KeyCreationResponse(
            key_id="generated-key-id",
            api_key="generated-api-key",
        )

    async def delete_key(self, key_id: str) -> None:
        return None

    async def get_tenants(self) -> list[TenantInfo]:
        return []


FAKE_API_KEY = "test-key-abc123"
FAKE_SUPERUSER_KEY = "superuser-key-xyz789"
FAKE_TENANT_ID = "tenant-test"
FAKE_API_KEY_NAME = "test-key"


def _zero_usage_metrics() -> UsageMetrics:
    """Return a zero-filled UsageMetrics for use in fake repository stubs."""
    return UsageMetrics(
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
    )


class FakeOrchestrator:
    """Fake orchestrator for testing.

    Returns configurable analyze and summarize results or raises a
    configurable exception.
    """

    def __init__(
        self,
        analyze_result=None,
        summarize_result=None,
        detect_sensitive_result=None,
        error=None,
    ):
        self._analyze_result = analyze_result or AnalysisResultModel(
            result="Fake analysis result",
        )
        self._summarize_result = summarize_result or SummaryResultModel(
            feedback_record_summaries=(
                FeedbackRecordSummaryModel(
                    id="doc-1",
                    title="Fake summary title",
                    summary="- Fake summary point",
                    quality_score=0.9,
                ),
            ),
        )
        self._detect_sensitive_result = (
            detect_sensitive_result
            or SensitivityAnalysisResultModelList(
                results=(
                    SensitivityAnalysisResultModel(
                        feedback_record_id="doc-1",
                        sensitivity_types=(SensitivityType.CORRUPTION,),
                        explanation="Contains a bribery allegation.",
                    ),
                ),
            )
        )
        self._error = error
        self.last_detect_sensitive_request = None

    async def analyze(
        self,
        request: AnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResultModel:
        if self._error is not None:
            raise self._error
        return self._analyze_result

    async def summarize(
        self,
        request: SummaryRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SummaryResultModel:
        if self._error is not None:
            raise self._error
        return self._summarize_result

    async def summarize_aggregate(
        self,
        request: SummaryRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AggregateSummaryResultModel:
        if self._error is not None:
            raise self._error
        return AggregateSummaryResultModel(
            ids=tuple(record.id for record in request.feedback_records),
            title="Fake aggregate title",
            summary="- Fake aggregate point",
            quality_score=0.9,
        )

    async def assign_codes(
        self,
        request: CodingAssignmentRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> CodingAssignmentResultModel:
        if self._error is not None:
            raise self._error
        return CodingAssignmentResultModel(
            coded_feedback_records=tuple(
                CodedFeedbackRecordModel(
                    feedback_record_id=record.id,
                    assigned_codes=(
                        AssignedCodeModel(
                            code_id="code-1",
                            code_label="Test code",
                            confidence_type=0.9,
                            confidence_category=0.85,
                            confidence_code=0.8,
                            confidence_aggregate=0.8,
                            explanation="Type: high. Category: good. Code: good.",
                        ),
                    ),
                )
                for record in request.feedback_records
            )
        )

    async def detect_sensitive_content(
        self,
        request: SensitivityAnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SensitivityAnalysisResultModelList:
        if self._error is not None:
            raise self._error
        self.last_detect_sensitive_request = request
        return self._detect_sensitive_result


class FakeUsageRepository(UsageRepositoryPort):
    """Minimal in-memory usage repository for API tests.

    Keeps the test app wiring aligned with production where ``usage_repo``
    is always present on ``app.state``.
    """

    async def record_call(self, record) -> None:
        return None

    async def get_usage_stats(self, tenant_id, from_=None, to=None):
        return UsageStats(
            tenant_id=tenant_id,
            total_calls=0,
            failed_calls=0,
            total_cost_usd=Decimal("0"),
            call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            llm_call_stats=_zero_usage_metrics(),
        )

    async def get_all_usage_by_tenant(self, from_=None, to=None):
        return [
            UsageStats(
                tenant_id="test-tenant-1",
                total_calls=0,
                failed_calls=0,
                total_cost_usd=Decimal("0"),
                call_duration=DistributionStats(
                    avg=0, min=0, max=0, p5=0, p95=0, total=0
                ),
                input_tokens=DistributionStats(
                    avg=0, min=0, max=0, p5=0, p95=0, total=0
                ),
                output_tokens=DistributionStats(
                    avg=0, min=0, max=0, p5=0, p95=0, total=0
                ),
                llm_call_stats=_zero_usage_metrics(),
            )
        ]

    async def get_all_usage_by_operation(self, from_=None, to=None):
        return [
            OperationUsageStats(
                operation=None,
                total_calls=0,
                failed_calls=0,
                total_cost_usd=Decimal("0"),
                call_duration=DistributionStats(
                    avg=0, min=0, max=0, p5=0, p95=0, total=0
                ),
                input_tokens=DistributionStats(
                    avg=0, min=0, max=0, p5=0, p95=0, total=0
                ),
                output_tokens=DistributionStats(
                    avg=0, min=0, max=0, p5=0, p95=0, total=0
                ),
                llm_call_stats=_zero_usage_metrics(),
            )
        ]


@pytest.fixture
def fake_api_keys():
    return [
        TenantApiKey(
            key_id=f"{FAKE_TENANT_ID}-0",
            name=FAKE_API_KEY_NAME,
            key=FAKE_API_KEY,  # type:ignore [ty:invalid-argument-type]
            hashed_key=None,  # type:ignore [ty:invalid-argument-type]
            tenant_id=FAKE_TENANT_ID,
            is_superuser=False,
        ),
        TenantApiKey(
            key_id=f"{FAKE_TENANT_ID}-1",
            name="Superuser 2",
            key=FAKE_SUPERUSER_KEY,  # type:ignore [ty:invalid-argument-type]
            hashed_key=None,  # type:ignore [ty:invalid-argument-type]
            tenant_id=FAKE_TENANT_ID,
            is_superuser=True,
        ),
    ]


@pytest.fixture
def fake_orchestrator():
    return FakeOrchestrator()


@pytest.fixture
def fake_auth_orchestrator(fake_api_keys):
    return AuthOrchestrator(
        auth_lookup_ports=[EnvironmentAuthLookupAdapter(api_keys=fake_api_keys)],
        auth_management_port=FakeAuthManagementPort(),
    )


@pytest.fixture
def test_app(fake_orchestrator, fake_auth_orchestrator):
    app = FastAPI(title="Test App")
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(usage_router)
    register_exception_handlers(app)

    app.state.orchestrator = fake_orchestrator
    app.state.auth_orchestrator = fake_auth_orchestrator
    app.state.usage_repo = FakeUsageRepository()

    return app


@pytest_asyncio.fixture
async def client(test_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
    ) as c:
        yield c
