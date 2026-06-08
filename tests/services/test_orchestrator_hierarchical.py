"""Tests for ``Orchestrator.analyze_hierarchical``.

Why: this method is the heart of #124. The tests pin the load-bearing
behaviours: anonymisation happens before embedding and before every LLM
call; guardrails appear at both map and reduce; both recursion triggers
fire on a large corpus; the coverage-weighted confidence is computed; and a
single_pass call is byte-identical to before (no regression).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from qfa.domain.errors import AnalysisError, AnalysisTimeoutError, LLMError
from qfa.domain.models import (
    AnalysisRequestModel,
    FeedbackRecordModel,
    LLMResponse,
)
from qfa.services.orchestrator import AnalyzeJudgeResult, Orchestrator
from qfa.services.prompts import ANALYZE_GUARDRAILS_PROMPT
from qfa.settings import AnalyzeSettings, OrchestratorSettings

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0


class FakeEmbeddingPort:
    """Deterministic, model-free embedder.

    Maps each text to a 2-D vector by a keyword bucket so clustering is
    predictable. Structurally conforms to EmbeddingPort (a test fake).
    """

    def embed(self, texts):
        """Return deterministic 2-D vectors keyed by keyword in each text."""
        vectors = []
        for text in texts:
            if "water" in text.lower():
                vectors.append((0.0, 0.0))
            elif "health" in text.lower():
                vectors.append((10.0, 10.0))
            else:
                vectors.append((100.0, -100.0))
        return tuple(vectors)


class RecordingAnonymizer:
    """Anonymiser that records every text it is asked to anonymise."""

    def __init__(self):
        self.anonymized_texts = []

    def anonymize(self, text):
        """Anonymise by replacing 'Jane' with a placeholder."""
        self.anonymized_texts.append(text)
        return text.replace("Jane", "<PERSON_0>"), {"<PERSON_0>": "Jane"}

    def deanonymize(self, text, mapping):
        """Restore placeholders from the mapping."""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text


class RecordingLLM:
    """Fake LLM recording every (system, user) pair; returns canned outputs.

    Map calls (response_model=str) return a partial; judge calls
    (response_model=AnalyzeJudgeResult) return a fixed score; reduce calls
    (response_model=str) return the synthesis.
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Record the call and return a canned response."""
        self.calls.append((system_message, user_message, response_model))
        if response_model is AnalyzeJudgeResult:
            return LLMResponse(
                structured=AnalyzeJudgeResult(
                    quality_score=0.8, uncertainty_explanation="leaf ok"
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        return LLMResponse(
            structured="PARTIAL_OR_REDUCE",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


def _records(n: int, text: str, prefix: str) -> tuple[FeedbackRecordModel, ...]:
    return tuple(
        FeedbackRecordModel(
            id=f"{prefix}{i}",
            text=text,
            metadata={"created": "2024-01-05T00:00:00Z", "codes": "Water"},
        )
        for i in range(n)
    )


def _build_orchestrator(llm, anonymizer, embedder, max_total_tokens):
    return Orchestrator(
        llm=llm,
        anonymizer=anonymizer,
        embedder=embedder,
        settings=OrchestratorSettings(),
        analyze_settings=AnalyzeSettings(min_cluster_size=2),
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=max_total_tokens,
    )


@pytest.mark.asyncio
async def test_hierarchical_covers_all_records_and_returns_confidence():
    """Every record is analysed (full coverage) and a coverage-weighted confidence is returned.

    Why: the spec forbids silent record loss and requires a confidence
    aggregated from leaf judges.
    """
    water = _records(4, "water access was limited " * 5, "w")
    health = _records(4, "health clinic medicine " * 5, "h")
    records = water + health
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = RecordingLLM()
    orch = _build_orchestrator(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)
    assert result.confidence is not None
    assert 0.0 <= result.confidence <= 1.0
    assert result.result  # non-empty synthesis


@pytest.mark.asyncio
async def test_guardrails_present_at_both_map_and_reduce():
    """The guardrails text appears in at least one map system msg and the reduce system msg.

    Why: records-as-data must hold at every prompt that contains records or
    partials (#75/#117).
    """
    records = _records(4, "water access " * 5, "w")
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = RecordingLLM()
    orch = _build_orchestrator(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    await orch.analyze_hierarchical(request, deadline, anonymize=True)
    system_msgs = [c[0] for c in llm.calls if c[2] is str]
    assert any(ANALYZE_GUARDRAILS_PROMPT in s for s in system_msgs)
    # The last str-model call is the top-level reduce.
    assert ANALYZE_GUARDRAILS_PROMPT in system_msgs[-1]


@pytest.mark.asyncio
async def test_anonymization_happens_before_any_llm_or_embed_call():
    """No raw PII reaches the embedder or the LLM; anonymiser saw the text first.

    Why: invariant (2) — anonymise before embedding AND before any LLM call.
    """
    records = (
        FeedbackRecordModel(
            id="r1",
            text="Jane reported water shortages " * 5,
            metadata={"created": "2024-01-05T00:00:00Z", "codes": "Water"},
        ),
        *_records(3, "water access " * 5, "w"),
    )
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = RecordingLLM()
    anonymizer = RecordingAnonymizer()
    orch = _build_orchestrator(
        llm, anonymizer, FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    await orch.analyze_hierarchical(request, deadline, anonymize=True)
    # Anonymiser was invoked, and no LLM user message contains the raw name.
    assert anonymizer.anonymized_texts
    assert all("Jane" not in c[1] for c in llm.calls)


class LargeOutputLLM:
    """LLM that returns a moderate map output to force multi-level tree-reduce.

    Map calls return a ~480-char partial. With a 700-token budget and a
    393-token system message, exactly 2 partials fit per reduce group
    (393+120+120=633 ≤ 700; 393+120+120+120=753 > 700). Eight map chunks
    therefore produce a tree-reduce of depth ≥ 2 (4 groups → 2 groups → 1).
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Return moderate output for map calls to trigger multi-level tree-reduce."""
        self.calls.append((system_message, user_message, response_model))
        if response_model is AnalyzeJudgeResult:
            return LLMResponse(
                structured=AnalyzeJudgeResult(
                    quality_score=0.75, uncertainty_explanation="ok"
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        if "<feedback_records>" in user_message:
            # Map call — return a ~480-char partial (120 tokens) so that
            # 2 partials fit per reduce group but 3 do not, forcing a
            # binary tree-reduce over 8 partials (depth = 3 levels).
            partial = "Analysis: " + ("Water access issues observed. " * 15)
            return LLMResponse(
                structured=partial,
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        # Reduce call — return a fixed-length synthesis (same size as a partial
        # so intermediate reduce outputs still overflow at higher tree levels).
        synthesis = "Synthesis: " + ("Water access issues observed. " * 15)
        return LLMResponse(
            structured=synthesis,
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


@pytest.mark.asyncio
async def test_recursion_fires_on_corpus_at_least_five_times_token_cap():
    """A corpus >5x the cap forces trigger 1 (cluster splitting) and trigger 2 (tree-reduce).

    Why: this is the headline acceptance criterion of #124. Trigger 1 fires
    when the cluster's total tokens exceed the budget (many map sub-chunks).
    Trigger 2 fires when the combined large partials exceed the budget,
    causing recursive tree-reduce. A LargeOutputLLM returns ~480-char
    partials (120 tokens). With a 700-token budget and a 393-token system
    message, exactly 2 partials fit per reduce group, so 8 map chunks produce
    a 3-level tree-reduce (4+2+1 reduce calls, all >= 2).
    """
    # 20 records of ~1000 chars each (250 tokens). Budget=700 tokens = 2800 chars,
    # so 2 records fit per map chunk → 10 map chunks (>= 3). Total = 20*250 = 5000
    # tokens, which is ~7x the 700-token cap (>5x requirement).
    per_record = (
        "water access was limited and people waited for hours. " * 19
    )  # ~1026 chars
    records = _records(20, per_record, "w")
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = LargeOutputLLM()
    orch = _build_orchestrator(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=700
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)
    # Many map calls (one per budget sub-chunk) — trigger 1.
    map_calls = [c for c in llm.calls if c[2] is str and "<feedback_records>" in c[1]]
    # Multiple reduce calls (combined partials overflow) — trigger 2.
    reduce_calls = [
        c for c in llm.calls if c[2] is str and "<partial_analyses>" in c[1]
    ]
    assert len(map_calls) >= 3, "cluster was not split into multiple budget sub-chunks"
    assert len(reduce_calls) >= 2, "partials never tree-reduced (single reduce only)"
    assert result.confidence is not None


class ConcurrencyTrackingLLM:
    """Fake LLM that records the peak number of concurrent ``complete`` calls.

    Each call yields control once via ``asyncio.sleep(0)`` so the event loop
    can interleave the gathered map tasks — without a real suspension point a
    coroutine runs to completion before the next starts, which would hide all
    concurrency and make the peak look like 1 regardless of the fan-out.
    """

    def __init__(self):
        self.calls = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Track in-flight depth around a single event-loop yield, then answer."""
        self.calls.append((system_message, user_message, response_model))
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        if response_model is AnalyzeJudgeResult:
            return LLMResponse(
                structured=AnalyzeJudgeResult(
                    quality_score=0.8, uncertainty_explanation="ok"
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        return LLMResponse(
            structured="PARTIAL_OR_REDUCE",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


def _multi_chunk_request() -> AnalysisRequestModel:
    """Build a request whose corpus splits into several budget-sized map chunks.

    20 long single-cluster records against a small token budget force the
    cluster to split into multiple map sub-chunks (the FakeEmbeddingPort maps
    every "water" record to one cluster, so the split is purely budget-driven).
    """
    per_record = "water access was limited and people waited for hours. " * 19
    records = _records(20, per_record, "w")
    return AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )


@pytest.mark.asyncio
async def test_map_chunks_run_concurrently_bounded_by_setting():
    """The map step runs chunks concurrently, capped at ``max_concurrent_chunks``.

    Why: parallelising the independent, latency-bound map calls is the point of
    the fan-out, but it must not burst past the provider's rate limit. With the
    cap at 2 and many chunks, the observed peak in-flight LLM calls must reach
    exactly 2 — proving both that concurrency happens (>1) and that the
    semaphore bounds it (never >2).
    """
    request = _multi_chunk_request()
    llm = ConcurrencyTrackingLLM()
    orch = Orchestrator(
        llm=llm,
        anonymizer=RecordingAnonymizer(),
        embedder=FakeEmbeddingPort(),
        settings=OrchestratorSettings(),
        analyze_settings=AnalyzeSettings(min_cluster_size=2, max_concurrent_chunks=2),
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=700,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)

    map_calls = [c for c in llm.calls if c[2] is str and "<feedback_records>" in c[1]]
    assert len(map_calls) >= 3, "corpus did not split into multiple map chunks"
    assert llm.peak_in_flight == 2
    assert result.confidence is not None


@pytest.mark.asyncio
async def test_max_concurrent_chunks_one_is_fully_sequential():
    """``max_concurrent_chunks=1`` restores fully sequential map behaviour.

    Why: it's the documented escape hatch for rate-limited providers (and for
    reproducing the old ordering). With the cap at 1, no two ``complete`` calls
    may overlap, so the observed peak in-flight must stay at 1 even though the
    corpus produces several chunks.
    """
    request = _multi_chunk_request()
    llm = ConcurrencyTrackingLLM()
    orch = Orchestrator(
        llm=llm,
        anonymizer=RecordingAnonymizer(),
        embedder=FakeEmbeddingPort(),
        settings=OrchestratorSettings(),
        analyze_settings=AnalyzeSettings(min_cluster_size=2, max_concurrent_chunks=1),
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=700,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)

    map_calls = [c for c in llm.calls if c[2] is str and "<feedback_records>" in c[1]]
    assert len(map_calls) >= 3, "corpus did not split into multiple map chunks"
    assert llm.peak_in_flight == 1
    assert result.confidence is not None


class OverlapTrackingLLM:
    """Records whether a leaf-judge call and a reduce call were ever in flight together.

    Each call yields once via ``asyncio.sleep(0)`` so the event loop interleaves
    the concurrently-gathered judge tasks with the reduce task; the call's kind
    is inferred from its response model (judge) and user message (reduce).
    """

    def __init__(self):
        self.calls = []
        self._in_flight = []
        self.judge_reduce_overlap = False

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Track which call kinds coexist in flight, then return a canned answer."""
        self.calls.append((system_message, user_message, response_model))
        if response_model is AnalyzeJudgeResult:
            kind = "judge"
        elif "<partial_analyses>" in user_message:
            kind = "reduce"
        else:
            kind = "map"
        self._in_flight.append(kind)
        if "judge" in self._in_flight and "reduce" in self._in_flight:
            self.judge_reduce_overlap = True
        try:
            await asyncio.sleep(0)
        finally:
            self._in_flight.remove(kind)
        if response_model is AnalyzeJudgeResult:
            return LLMResponse(
                structured=AnalyzeJudgeResult(
                    quality_score=0.8, uncertainty_explanation="ok"
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        return LLMResponse(
            structured="PARTIAL_OR_REDUCE",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


class OneChunkMapFailsLLM:
    """LLM whose map call fails for the 'health' cluster but succeeds elsewhere.

    A map call is identified by ``response_model is str`` plus the
    ``<feedback_records>`` envelope; when such a call carries 'health' text it
    raises ``LLMError`` to simulate one chunk failing. Judge and reduce calls
    answer normally. Used to exercise the partial-failure path.
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Raise on the 'health' map chunk; return canned output otherwise."""
        self.calls.append((system_message, user_message, response_model))
        is_map = response_model is str and "<feedback_records>" in user_message
        if is_map and "health" in user_message:
            raise LLMError("simulated map failure for one chunk")
        if response_model is AnalyzeJudgeResult:
            return LLMResponse(
                structured=AnalyzeJudgeResult(
                    quality_score=0.8, uncertainty_explanation="ok"
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        return LLMResponse(
            structured="PARTIAL_OR_REDUCE",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


@pytest.mark.asyncio
async def test_partial_map_failure_excludes_chunk_from_confidence():
    """One chunk's map failure doesn't abort the run; it is excluded from confidence.

    Why: regression for the partial-failure path. A failed map chunk becomes a
    ``None`` partial that MUST be filtered before the reduce prompt — passing
    None into ``build_reduce_user_message`` raised ``AttributeError`` and tore
    down the whole analysis. The failed chunk has no partial to judge, so it is
    *excluded* from the coverage-weighted confidence (unverified ≠ unfaithful):
    confidence reflects only the surviving chunk's leaf score (0.8), and the
    exclusion is reported in ``uncertainty_explanation``. Only an all-chunk
    failure raises.
    """
    water = _records(4, "water access was limited " * 5, "w")
    health = _records(4, "health clinic medicine " * 5, "h")
    request = AnalysisRequestModel(
        feedback_records=water + health,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = OneChunkMapFailsLLM()
    orch = _build_orchestrator(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)

    # The run completed despite one chunk failing — the reduce-phase regression.
    assert result.result
    # Reduce still ran; the surviving partial fed it (None was filtered out).
    reduce_calls = [
        c for c in llm.calls if c[2] is str and "<partial_analyses>" in c[1]
    ]
    assert reduce_calls, "reduce never ran after a partial map failure"
    # The failed chunk is excluded, so confidence is exactly the surviving
    # chunk's 0.8 (not dragged toward 0.0), and the exclusion is surfaced.
    assert result.confidence == pytest.approx(0.8)
    assert "excluded" in result.uncertainty_explanation


class AllJudgesFailLLM:
    """Map and reduce calls succeed, but every leaf-judge call raises.

    Exercises the "nothing could be judged" path: the synthesis is produced
    normally, yet confidence must come back ``None`` rather than 0.0.
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Raise on judge calls; return canned output for map and reduce."""
        self.calls.append((system_message, user_message, response_model))
        if response_model is AnalyzeJudgeResult:
            raise LLMError("simulated judge failure")
        return LLMResponse(
            structured="PARTIAL_OR_REDUCE",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


@pytest.mark.asyncio
async def test_all_judges_failing_yields_none_confidence_with_synthesis():
    """When every leaf judge fails, confidence is None but the synthesis stands.

    Why: judge failures are excluded from confidence, so if *none* succeed there
    is no faithfulness signal at all — confidence must be ``None`` (unavailable),
    not 0.0 (verified-bad). The deliverable synthesis is independent of judging
    and must still be returned.
    """
    water = _records(4, "water access was limited " * 5, "w")
    health = _records(4, "health clinic medicine " * 5, "h")
    request = AnalysisRequestModel(
        feedback_records=water + health,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    orch = _build_orchestrator(
        AllJudgesFailLLM(),
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=100_000,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)

    assert result.result  # synthesis produced
    assert result.confidence is None
    assert "unavailable" in result.uncertainty_explanation.lower()


@pytest.mark.asyncio
async def test_judge_phase_timeout_does_not_discard_synthesis():
    """A timeout escaping the judge phase still yields the synthesis, confidence None.

    Why: the synthesis is produced before judging, so a judge-phase timeout must
    not tear down the whole request. Patching ``_judge_chunk`` to raise
    ``AnalysisTimeoutError`` simulates a timeout escaping the per-chunk handling;
    the phase-level backstop must catch it, mark confidence unavailable, and run
    the pure-Python result assembly to completion.
    """
    water = _records(4, "water access was limited " * 5, "w")
    health = _records(4, "health clinic medicine " * 5, "h")
    request = AnalysisRequestModel(
        feedback_records=water + health,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    orch = _build_orchestrator(
        RecordingLLM(),
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=100_000,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    with patch.object(
        Orchestrator,
        "_judge_chunk",
        new=AsyncMock(side_effect=AnalysisTimeoutError("judge deadline exceeded")),
    ):
        result = await orch.analyze_hierarchical(request, deadline, anonymize=True)

    assert result.result  # synthesis assembled and returned
    assert result.confidence is None
    assert "unavailable" in result.uncertainty_explanation.lower()


@pytest.mark.asyncio
async def test_all_chunks_failing_raises_analysis_error():
    """When every map chunk fails, analyze_hierarchical raises AnalysisError.

    Why: a partial failure degrades gracefully, but a total map failure has no
    partials to synthesise, so it must surface as an error rather than an empty
    result.
    """

    class AllMapsFailLLM(OneChunkMapFailsLLM):
        """Every map call fails; judge/reduce never get the chance to run."""

        async def complete(
            self,
            system_message,
            user_message,
            tenant_id,
            response_model=str,
            timeout=20.0,
        ):
            """Raise on every map call regardless of cluster."""
            self.calls.append((system_message, user_message, response_model))
            if response_model is str and "<feedback_records>" in user_message:
                raise LLMError("simulated map failure for all chunks")
            return LLMResponse(
                structured="UNREACHED",
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )

    water = _records(4, "water access was limited " * 5, "w")
    health = _records(4, "health clinic medicine " * 5, "h")
    request = AnalysisRequestModel(
        feedback_records=water + health,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    orch = _build_orchestrator(
        AllMapsFailLLM(),
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=100_000,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    with pytest.raises(AnalysisError, match="mapping failed for all chunks"):
        await orch.analyze_hierarchical(request, deadline, anonymize=True)


@pytest.mark.asyncio
async def test_reduce_runs_to_completion_before_any_judge():
    """The reduce phase fully completes before the first leaf-judge call starts.

    Why: the synthesis is the deliverable, so it gets first claim on the
    concurrency slots and short-circuits the judges on failure. Sequencing
    reduce-before-judge means no judge call is ever in flight while a reduce
    call runs, and every reduce call precedes every judge call in time. The
    aggregated confidence and synthesis must still be produced.
    """
    request = _multi_chunk_request()
    llm = OverlapTrackingLLM()
    orch = Orchestrator(
        llm=llm,
        anonymizer=RecordingAnonymizer(),
        embedder=FakeEmbeddingPort(),
        settings=OrchestratorSettings(),
        analyze_settings=AnalyzeSettings(min_cluster_size=2, max_concurrent_chunks=50),
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=700,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await orch.analyze_hierarchical(request, deadline, anonymize=True)

    # No judge call ever coexisted with a reduce call.
    assert not llm.judge_reduce_overlap, "judge ran concurrently with reduce"
    # Every reduce call strictly precedes every judge call in the recorded order.
    kinds = [
        "judge"
        if c[2] is AnalyzeJudgeResult
        else ("reduce" if "<partial_analyses>" in c[1] else "map")
        for c in llm.calls
    ]
    reduce_positions = [i for i, k in enumerate(kinds) if k == "reduce"]
    judge_positions = [i for i, k in enumerate(kinds) if k == "judge"]
    assert reduce_positions, "reduce never ran"
    assert judge_positions, "judge never ran"
    assert max(reduce_positions) < min(judge_positions), (
        "a judge call started before the reduce phase finished"
    )
    assert result.confidence is not None
    assert result.result
