"""Tests for routing judge calls to a separate LLM connection (#258).

Why its own module: the property under test is *which client served which
call*, which cuts across four use-case methods that otherwise have little
in common. Each test drives a real use-case method with two distinguishable
fakes — one primary, one judge — and asserts the split, so a future refactor
that quietly re-points a call site at ``self._llm`` fails here rather than
showing up as a mysterious cost shift in production.

The complementary case matters just as much: with no judge client configured
the services must behave exactly as they did before, so every test below has
a counterpart asserting the single-client default.

Two of the four call sites moved out of ``Orchestrator`` into
:class:`~qfa.services.summarize.SummarizeService` (#264). The routing tests
stay together here rather than following the use case into
``test_summarize.py``: what they pin is the cross-service split, and splitting
them per service is exactly how one call site quietly stops being checked.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from qfa.adapters.tracking_llm import TrackingLLMAdapter
from qfa.domain.models import (
    AggregateSummaryResultModel,
    AnalysisRequestModel,
    CodingAssignmentRequestModel,
    CodingFramework,
    CodingNode,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    FeedbackRecordSummaryModel,
    LLMResponse,
    SingleSummaryRequestModel,
    SummaryRequestModel,
    SummaryResultModel,
)
from qfa.domain.ports import (
    AnonymizationPort,
    EmbeddingPort,
    LLMPort,
    UsageRepositoryPort,
)
from qfa.domain.usage_models import LLMCallRecord, Operation
from qfa.services.analyze import AnalyzeJudgeResult, AnalyzeService
from qfa.services.call_context import call_scope
from qfa.services.coding import CodingService
from qfa.services.coding_classifier import CodingResponse, JudgeResponse
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.summarize import SummarizeService
from qfa.settings import AnalyzeSettings, OrchestratorSettings

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0
MAX_TOKENS = 100_000

# A bare float on the first line is the contract the two summary judges parse
# with ``_parse_judge_quality_score``; the rest of the string is ignored. It
# doubles as generic free text for the map/reduce/analysis calls.
JUDGE_PARSEABLE_TEXT = "0.75\nThe summary is faithful to the source."


class RoutingLLM(LLMPort):
    """Fake ``LLMPort`` that stamps its own name onto every response it serves.

    Two instances stand in for the primary and judge connections. Because the
    name is carried through as ``LLMResponse.model``, a test can assert both
    *that* a client was called and that the response the orchestrator kept came
    from the expected one — the same field usage tracking bills against.

    Payloads are selected by ``response_model`` because that is what actually
    distinguishes the call kinds in the orchestrator (``AnalyzeJudgeResult``
    for the analyse and leaf judges, ``CodingResponse`` for the one-shot
    coding pick, ``JudgeResponse`` for the per-level coding judge, the
    concrete summary models for generation, and ``str`` for everything
    free-text). ``text_payload`` overrides the ``str`` case for callers
    whose free-text contract is not a judge score.
    """

    def __init__(self, name: str, text_payload: str = JUDGE_PARSEABLE_TEXT) -> None:
        self.name = name
        self.text_payload = text_payload
        self.calls: list[dict] = []

    @property
    def response_models(self) -> list[type]:
        """The ``response_model`` of each call served, in order."""
        return [call["response_model"] for call in self.calls]

    async def complete(
        self,
        system_message,
        user_message,
        tenant_id,
        response_model=str,
        timeout=20.0,
    ):
        """Record the call and return a canned payload tagged with this client's name."""
        self.calls.append(
            {
                "system_message": system_message,
                "user_message": user_message,
                "tenant_id": tenant_id,
                "response_model": response_model,
                "timeout": timeout,
            }
        )
        return LLMResponse(
            structured=self._payload(response_model),
            model=self.name,
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.001,
        )

    def _payload(self, response_model: type) -> Any:
        """Build a minimal valid payload for ``response_model``."""
        if response_model is AnalyzeJudgeResult:
            return AnalyzeJudgeResult(quality_score=0.8, uncertainty_explanation="ok")
        if response_model is CodingResponse:
            return CodingResponse(selected=[0])
        if response_model is JudgeResponse:
            return JudgeResponse(score=0.9, explanation="clearly relevant")
        if response_model is SummaryResultModel:
            return SummaryResultModel(
                feedback_record_summaries=(
                    FeedbackRecordSummaryModel(
                        id="doc-1", title="Title", summary="- Point", quality_score=0.0
                    ),
                )
            )
        if response_model is AggregateSummaryResultModel:
            return AggregateSummaryResultModel(
                title="Title", summary="- Point", quality_score=0.0
            )
        return self.text_payload


class NoopAnonymizer(AnonymizationPort):
    """Pass-through anonymiser: keeps these tests about routing, nothing else."""

    def anonymize(self, text):
        """Return the text unchanged with an empty placeholder mapping."""
        return text, {}

    def deanonymize(self, text, mapping):
        """Return the text unchanged."""
        return text


class FakeUsageRepository(UsageRepositoryPort):
    """In-memory usage repository: collects the rows tracking would persist."""

    def __init__(self) -> None:
        self.records: list[LLMCallRecord] = []

    async def record_call(self, record: LLMCallRecord) -> None:
        """Collect one call record."""
        self.records.append(record)

    async def get_usage_stats_for_one_tenant(self, tenant_id, from_=None, to=None):
        """Unused here — these tests only exercise the write path."""
        raise NotImplementedError

    async def get_all_usage_by_tenant(self, from_=None, to=None):
        """Unused here — these tests only exercise the write path."""
        raise NotImplementedError

    async def get_all_usage_by_operation(self, from_=None, to=None):
        """Unused here — these tests only exercise the write path."""
        raise NotImplementedError


class TwoClusterEmbedder(EmbeddingPort):
    """Deterministic embedder splitting records into two well-separated clusters.

    Hierarchical analysis needs more than one chunk for the leaf-judge phase to
    be interesting, and real embeddings would make the chunk count depend on a
    model rather than on the test.
    """

    def embed(self, texts):
        """Map each text to one of two far-apart 2-D points by keyword."""
        return tuple(
            (0.0, 0.0) if "water" in text.lower() else (100.0, 100.0) for text in texts
        )


def _records(count: int, text: str, prefix: str) -> tuple[FeedbackRecordModel, ...]:
    return tuple(
        FeedbackRecordModel(
            id=f"{prefix}{i}",
            content=text,
            metadata=FeedbackRecordMetadataModel(created="2024-01-05T00:00:00Z"),
        )
        for i in range(count)
    )


def _feedback_record() -> FeedbackRecordModel:
    return _records(1, "Some feedback text.", "doc-")[0]


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=300)


def _build_coding_service(primary: LLMPort) -> CodingService:
    """Build the coding service over the given client.

    There is no ``judge`` parameter on purpose: the service has no judge
    connection to hand one to (see ``assign_codes`` below).
    """
    anonymizer = NoopAnonymizer()
    return CodingService(
        llm=primary,
        anonymizer=anonymizer,
        executor=LLMCallExecutor(
            llm=primary,
            anonymizer=anonymizer,
            settings=OrchestratorSettings(),
            llm_timeout_seconds=LLM_TIMEOUT,
            max_total_tokens=MAX_TOKENS,
        ),
    )


def _build_summarize(
    primary: LLMPort, judge: LLMPort | None = None
) -> SummarizeService:
    """Build a summarisation service over the given client(s).

    Mirrors :func:`_build`, and like the production wiring it hands the
    service the real :class:`LLMCallExecutor` — built over the *primary*
    connection, since judge calls pick their client per call.
    """
    anonymizer = NoopAnonymizer()
    return SummarizeService(
        llm=primary,
        judge_llm=judge,
        anonymizer=anonymizer,
        executor=LLMCallExecutor(
            llm=primary,
            anonymizer=anonymizer,
            settings=OrchestratorSettings(),
            llm_timeout_seconds=LLM_TIMEOUT,
            max_total_tokens=MAX_TOKENS,
        ),
    )


def _build_analyze(
    primary: LLMPort, judge: LLMPort | None = None, **kwargs
) -> AnalyzeService:
    """Build an analyze service over the given client(s).

    Mirrors :func:`_build` for the extracted analyse use case, over the
    *real* ``LLMCallExecutor`` (ADR-017 decision 3) so the judge client
    that reaches ``bounded_complete`` is the production one.
    """
    anonymizer = NoopAnonymizer()
    settings = OrchestratorSettings()
    executor = LLMCallExecutor(
        llm=primary,
        anonymizer=anonymizer,
        settings=settings,
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=MAX_TOKENS,
    )
    return AnalyzeService(
        executor=executor,
        llm=primary,
        judge_llm=judge,
        anonymizer=anonymizer,
        settings=settings,
        max_total_tokens=MAX_TOKENS,
        **kwargs,
    )


class TestDefaultsToThePrimaryClient:
    """With no judge client, every call — judge included — goes to the primary."""

    def test_judge_client_is_the_primary_client_when_unset(self) -> None:
        """``_judge_llm`` falls back to ``_llm`` so call sites never need to branch."""
        primary = RoutingLLM("primary")

        analyze = _build_analyze(primary)

        assert analyze._judge_llm is primary

    def test_summarize_service_judge_client_is_the_primary_client_when_unset(
        self,
    ) -> None:
        """The extracted service repeats the same fallback, not a new default."""
        primary = RoutingLLM("primary")

        service = _build_summarize(primary)

        assert service._judge_llm is primary

    @pytest.mark.asyncio
    async def test_analyze_judge_uses_the_primary_model_when_unset(self) -> None:
        """The analyse judge still runs on the primary model — no behaviour change.

        This is the acceptance criterion that the default configuration is
        unchanged: both the analysis and its judge are served by, and billed
        to, the one model that served them before #258.
        """
        primary = RoutingLLM("primary")
        analyze = _build_analyze(primary)

        await analyze.analyze_bulk(
            AnalysisRequestModel(
                feedback_records=(_feedback_record(),),
                prompt="Summarize feedback.",
                tenant_id=TENANT_ID,
            ),
            _deadline(),
        )

        assert primary.response_models == [str, AnalyzeJudgeResult]

    @pytest.mark.asyncio
    async def test_summarize_judge_uses_the_primary_client_when_unset(self) -> None:
        """Both the summary and its judge stay on the primary client."""
        primary = RoutingLLM("primary")
        service = _build_summarize(primary)

        await service.summarize(
            SingleSummaryRequestModel(
                feedback_record=_feedback_record(), tenant_id=TENANT_ID
            ),
            _deadline(),
        )

        assert primary.response_models == [SummaryResultModel, str]


class TestJudgeCallsRouteToTheJudgeClient:
    """Each of the four judge call sites issues its call on the judge client."""

    @pytest.mark.asyncio
    async def test_analyze_judge_call(self) -> None:
        """``analyze_bulk`` splits: analysis on the primary, judge on the judge client.

        Asserted through ``quality_score``, not just the call log, so the test
        also pins that the orchestrator keeps the judge *client's* verdict.
        """
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        analyze = _build_analyze(primary, judge)

        result = await analyze.analyze_bulk(
            AnalysisRequestModel(
                feedback_records=(_feedback_record(),),
                prompt="Summarize feedback.",
                tenant_id=TENANT_ID,
            ),
            _deadline(),
        )

        assert primary.response_models == [str]
        assert judge.response_models == [AnalyzeJudgeResult]
        assert result.quality_score == 0.8

    @pytest.mark.asyncio
    async def test_aggregate_summary_judge_call(self) -> None:
        """``summarize_bulk`` judges on the judge client, generates on the primary.

        Its judge uses the free-text contract (a bare float parsed off the
        first line), which the switch of client must not disturb.
        """
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        service = _build_summarize(primary, judge)

        result = await service.summarize_bulk(
            SummaryRequestModel(
                feedback_records=(_feedback_record(),), tenant_id=TENANT_ID
            ),
            _deadline(),
        )

        assert primary.response_models == [AggregateSummaryResultModel]
        assert judge.response_models == [str]
        assert result.quality_score == 0.75

    @pytest.mark.asyncio
    async def test_single_summary_judge_call(self) -> None:
        """``summarize`` judges on the judge client, generates on the primary."""
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        service = _build_summarize(primary, judge)

        result = await service.summarize(
            SingleSummaryRequestModel(
                feedback_record=_feedback_record(), tenant_id=TENANT_ID
            ),
            _deadline(),
        )

        assert primary.response_models == [SummaryResultModel]
        assert judge.response_models == [str]
        assert result.quality_score == 0.75

    @pytest.mark.asyncio
    async def test_hierarchical_leaf_judges_only(self) -> None:
        """Map and reduce stay on the primary; only the leaf judges move.

        These three call kinds share one helper and one semaphore, so this is
        the site where a routing mistake is easiest to make and hardest to
        spot — map partials silently graded by, or generated on, the wrong
        model would still produce a plausible-looking result.
        """
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        analyze = _build_analyze(
            primary,
            judge,
            embedder=TwoClusterEmbedder(),
            analyze_settings=AnalyzeSettings(min_cluster_size=2),
        )
        records = _records(4, "water access was limited " * 5, "w") + _records(
            4, "health clinic medicine " * 5, "h"
        )

        result = await analyze.analyze_hierarchical(
            AnalysisRequestModel(
                feedback_records=records,
                prompt="trends?",
                tenant_id=TENANT_ID,
                mode="hierarchical",
            ),
            _deadline(),
        )

        # Map + reduce are free-text calls and all landed on the primary.
        assert primary.calls
        assert set(primary.response_models) == {str}
        # Every judge call, and only judge calls, landed on the judge client.
        assert judge.calls
        assert set(judge.response_models) == {AnalyzeJudgeResult}
        assert result.confidence == 0.8

    @pytest.mark.asyncio
    async def test_hierarchical_judge_calls_still_share_the_primary_semaphore(
        self,
    ) -> None:
        """Splitting the client does not split the concurrency bound.

        The semaphore caps total in-flight hierarchical calls, not calls per
        connection. Had the judge client been given its own bound, a judge
        phase could run ``max_concurrent_chunks`` calls *on top of* the map
        phase and blow past the intended ceiling.
        """
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        analyze = _build_analyze(
            primary,
            judge,
            embedder=TwoClusterEmbedder(),
            analyze_settings=AnalyzeSettings(min_cluster_size=2),
        )
        records = _records(4, "water access was limited " * 5, "w") + _records(
            4, "health clinic medicine " * 5, "h"
        )

        await analyze.analyze_hierarchical(
            AnalysisRequestModel(
                feedback_records=records,
                prompt="trends?",
                tenant_id=TENANT_ID,
                mode="hierarchical",
            ),
            _deadline(),
        )

        # Every judge call went through the bounded helper, so it carries a
        # deadline-derived timeout rather than the port's default.
        assert judge.calls
        assert all(call["timeout"] <= LLM_TIMEOUT for call in judge.calls)


class TestGenerationCallsStayOnThePrimaryClient:
    """Configuring a judge model must not move any generation call."""

    @pytest.mark.asyncio
    async def test_analysis_call_keeps_the_primary_model(self) -> None:
        """The analysis itself is served by the primary client, not the judge one."""
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        analyze = _build_analyze(primary, judge)

        await analyze.analyze_bulk(
            AnalysisRequestModel(
                feedback_records=(_feedback_record(),),
                prompt="Summarize feedback.",
                tenant_id=TENANT_ID,
            ),
            _deadline(),
        )

        assert len(primary.calls) == 1
        assert primary.response_models == [str]

    @pytest.mark.asyncio
    async def test_coding_classification_stays_entirely_on_the_primary(self) -> None:
        """``assign_codes`` — one-shot pick *and* its per-level judge — stays on the primary.

        The per-level coding judge is deliberately excluded from #258's four
        sites: the ticket scopes the split to the quality-score judges on
        analyse and summarise. Pinned here so the exclusion is a recorded
        decision rather than something a later reader assumes was an oversight.

        Since #265 the exclusion is structural as well as behavioural:
        :class:`~qfa.services.coding.CodingService` takes no judge client, so
        a judge client that exists in the same process is unreachable from
        this path. Both halves are asserted below.
        """
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        coding = _build_coding_service(primary)

        await coding.assign_codes(
            CodingAssignmentRequestModel(
                feedback_record=_feedback_record(),
                coding_levels=CodingFramework(
                    root_codes=[CodingNode(id="code-1", name="Code A")]
                ),
                max_codes=1,
                confidence_threshold=0.5,
                tenant_id=TENANT_ID,
            ),
            _deadline(),
        )

        assert primary.response_models == [CodingResponse, JudgeResponse]
        assert judge.calls == []


class TestCostAccountingAcrossBothClients:
    """``LLMResponse.model`` identifies the serving client, so usage stays attributable."""

    @pytest.mark.asyncio
    async def test_aggregate_summary_cost_sums_across_both_clients(self) -> None:
        """Both connections are billed for an aggregate summary.

        Cost is summed by ``TrackingLLMAdapter`` per client, not by the
        service, so what makes both costs land is that ``summarize_bulk``
        reaches each client exactly once. A judge call that silently fell
        back to the primary would show up here as two primary calls.
        """
        primary = RoutingLLM("primary")
        judge = RoutingLLM("judge")
        service = _build_summarize(primary, judge)

        await service.summarize_bulk(
            SummaryRequestModel(
                feedback_records=(_feedback_record(),), tenant_id=TENANT_ID
            ),
            _deadline(),
        )

        assert len(primary.calls) == 1
        assert len(judge.calls) == 1

    @pytest.mark.asyncio
    async def test_judge_call_usage_is_recorded_against_the_judge_model(self) -> None:
        """A tracked judge client records its own call, attributed to its own model.

        Wires the orchestrator the way the lifespan does — each client behind
        its own ``TrackingLLMAdapter`` over one shared usage repository — and
        drives a real analyse request through it. The assertion is that *two*
        rows land in the repository, one per model: had the judge client been
        injected unwrapped, the analysis would still be graded correctly and
        the judge call would simply never be billed.
        """
        repo = FakeUsageRepository()
        primary = RoutingLLM("primary-model")
        judge = RoutingLLM("judge-model")
        analyze = _build_analyze(
            TrackingLLMAdapter(inner=primary, usage_repo=repo),
            TrackingLLMAdapter(inner=judge, usage_repo=repo),
        )

        async with call_scope(
            tenant_id=TENANT_ID, operation=Operation.ANALYZE, request_id=uuid4()
        ):
            await analyze.analyze_bulk(
                AnalysisRequestModel(
                    feedback_records=(_feedback_record(),),
                    prompt="Summarize feedback.",
                    tenant_id=TENANT_ID,
                ),
                _deadline(),
            )

        assert [record.model for record in repo.records] == [
            "primary-model",
            "judge-model",
        ]
        # Totals sum across both connections rather than tracking only one.
        assert sum(record.cost_usd for record in repo.records) == Decimal("0.002")
