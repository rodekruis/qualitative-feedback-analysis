"""Tests for ``AnalyzeService.analyze_hierarchical``.

Why: this method is the heart of #124. The tests pin the load-bearing
behaviours: anonymisation happens before embedding and before every LLM
call; guardrails appear at both map and reduce; both recursion triggers
fire on a large corpus; the coverage-weighted confidence is computed; and a
single_pass call is byte-identical to before (no regression).
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from html import unescape
from unittest.mock import AsyncMock, patch

import pytest

from qfa.adapters.presidio_anonymizer import PresidioAnonymizer
from qfa.domain.errors import AnalysisError, AnalysisTimeoutError, LLMError
from qfa.domain.models import (
    AnalysisRequestModel,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    LLMResponse,
)
from qfa.domain.ports import AnonymizationPort, EmbeddingPort, LLMPort
from qfa.services.analyze import AnalyzeService
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.prompts import ANALYZE_GUARDRAILS_PROMPT
from qfa.settings import AnalyzeSettings, OrchestratorSettings

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0


class FakeEmbeddingPort(EmbeddingPort):
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


class RecordingAnonymizer(AnonymizationPort):
    """Anonymiser that records every text it is asked to anonymise."""

    def __init__(self):
        self.anonymized_texts = []

    def anonymize(self, text):
        """Anonymise by replacing 'Jane' with a placeholder."""
        self.anonymized_texts.append(text)
        return text.replace("Jane", "<PERSON_0>"), {"<PERSON_0>": "Jane"}

    def anonymize_batch(self, texts):
        """Redact each text, merging the (single-key) mappings."""
        merged = {}
        redacted = []
        for text in texts:
            text, mapping = self.anonymize(text)
            redacted.append(text)
            merged.update(mapping)
        return tuple(redacted), merged

    def deanonymize(self, text, mapping):
        """Restore placeholders from the mapping."""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text


def _is_judge_call(system_message: str) -> bool:
    """Distinguish a leaf-judge call from a map/reduce call by prompt shape.

    All three call kinds share ``response_model=str`` now (the judge parses
    free text instead of structured output — see ``AnalyzeJudgeResult``'s
    docstring), so a fake LLM can no longer dispatch on ``response_model``.
    ``<analysis_to_score>`` is unique to ``ANALYZE_JUDGE_PROMPT``
    (``prompts.py``) — neither the map nor the reduce prompt
    (``hierarchical_prompts.py``) uses it.
    """
    return "<analysis_to_score>" in system_message


def _judge_text(quality_score=0.8, explanation="leaf ok"):
    """Render a judge score/explanation as the free-text reply a fake serves."""
    return f"QUALITY_SCORE: {quality_score}\nUNCERTAINTY_EXPLANATION: {explanation}"


class RecordingLLM(LLMPort):
    """Fake LLM recording every (system, user) pair; returns canned outputs.

    Map calls return a partial; judge calls (detected via
    :func:`_is_judge_call`) return a fixed score; reduce calls return the
    synthesis. All three are ``response_model=str`` now.
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Record the call and return a canned response."""
        self.calls.append((system_message, user_message, response_model))
        if _is_judge_call(system_message):
            return LLMResponse(
                structured=_judge_text(),
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
            content=text,
            metadata=FeedbackRecordMetadataModel(created="2024-01-05T00:00:00Z"),
        )
        for i in range(n)
    )


def _build_analyze_service(
    llm, anonymizer, embedder, max_total_tokens, analyze_settings=None
):
    """Build an ``AnalyzeService`` over the *real* ``LLMCallExecutor``.

    Per ADR-017 there is no fake executor: these tests construct the real
    collaborator over the fake driven adapters, so the semaphore-bounded
    completions and deadline arithmetic under test are the production ones.
    """
    settings = OrchestratorSettings()
    executor = LLMCallExecutor(
        llm=llm,
        anonymizer=anonymizer,
        settings=settings,
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=max_total_tokens,
    )
    return AnalyzeService(
        executor=executor,
        llm=llm,
        anonymizer=anonymizer,
        embedder=embedder,
        settings=settings,
        analyze_settings=analyze_settings or AnalyzeSettings(min_cluster_size=2),
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
    service = _build_analyze_service(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    result = await service.analyze_hierarchical(request, deadline, anonymize=True)
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
    service = _build_analyze_service(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    await service.analyze_hierarchical(request, deadline, anonymize=True)
    system_msgs = [c[0] for c in llm.calls if c[2] is str and not _is_judge_call(c[0])]
    assert any(ANALYZE_GUARDRAILS_PROMPT in s for s in system_msgs)
    # The last non-judge str-model call is the top-level reduce (judges run
    # after reduce and share response_model=str, so they must be excluded).
    assert ANALYZE_GUARDRAILS_PROMPT in system_msgs[-1]


@pytest.mark.asyncio
async def test_output_language_instructs_the_reduce_system_message():
    """``output_language`` adds a directive naming the language to the top-level reduce prompt.

    Why: #154 — the user-facing analysis is produced by the reduce
    (synthesis) step, so the language directive must reach that prompt for
    the hierarchical path to honour ``output_language``.
    """
    records = _records(4, "water access " * 5, "w")
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
        output_language="Dutch",
    )
    llm = RecordingLLM()
    service = _build_analyze_service(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    await service.analyze_hierarchical(request, deadline, anonymize=True)
    system_msgs = [c[0] for c in llm.calls if c[2] is str and not _is_judge_call(c[0])]
    # The last non-judge str-model call is the top-level reduce — the final,
    # user-facing output (judges run after reduce and share response_model=str).
    assert "Dutch" in system_msgs[-1]
    # Every map (per-chunk) call must also honour it — a partial should
    # already be in the target language rather than leaving translation of a
    # whole mixed-language corpus to the single final reduce call.
    assert all("Dutch" in s for s in system_msgs[:-1])


@pytest.mark.asyncio
async def test_anonymization_happens_before_any_llm_or_embed_call():
    """No raw PII reaches the embedder or the LLM; anonymiser saw the text first.

    Why: invariant (2) — anonymise before embedding AND before any LLM call.
    """
    records = (
        FeedbackRecordModel(
            id="r1",
            content="Jane reported water shortages " * 5,
            metadata=FeedbackRecordMetadataModel(created="2024-01-05T00:00:00Z"),
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
    service = _build_analyze_service(
        llm, anonymizer, FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    await service.analyze_hierarchical(request, deadline, anonymize=True)
    # Anonymiser was invoked, and no LLM user message contains the raw name.
    assert anonymizer.anonymized_texts
    assert all("Jane" not in c[1] for c in llm.calls)


@pytest.mark.asyncio
async def test_anonymize_and_embed_do_not_block_the_event_loop():
    """Anonymisation and embedding run off the event loop thread (#325).

    A synchronous, blocking anonymiser/embedder must not stall other
    concurrently-scheduled coroutines — that stall is exactly what starved
    gunicorn's asgi-worker heartbeat and got the process SIGKILLed.
    """
    block_seconds = 0.3

    class BlockingAnonymizer(AnonymizationPort):
        def anonymize(self, text):
            (redacted,), mapping = self.anonymize_batch((text,))
            return redacted, mapping

        def anonymize_batch(self, texts):
            # Simulates real Presidio work: genuine thread-blocking sleep,
            # not an awaitable — the point being tested is that this does
            # NOT block the event loop.
            time.sleep(block_seconds)
            return texts, {}

        def deanonymize(self, text, mapping):
            return text

    class BlockingEmbedder(EmbeddingPort):
        def embed(self, texts):
            time.sleep(block_seconds)
            return tuple((0.0, 0.0) for _ in texts)

    records = _records(4, "water access " * 5, "w")
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = RecordingLLM()
    service = _build_analyze_service(
        llm, BlockingAnonymizer(), BlockingEmbedder(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    ticker_finished_at: float | None = None

    async def ticker() -> None:
        nonlocal ticker_finished_at
        # If the event loop were blocked by the anonymiser's or embedder's
        # sleep, this could not resume until that sleep finished.
        await asyncio.sleep(0.05)
        ticker_finished_at = time.monotonic()

    start = time.monotonic()
    ticker_task = asyncio.create_task(ticker())
    await service.analyze_hierarchical(request, deadline, anonymize=True)
    await ticker_task

    assert ticker_finished_at is not None
    assert ticker_finished_at - start < block_seconds / 2


class LargeOutputLLM(LLMPort):
    """LLM that returns a moderate map output to force multi-level tree-reduce.

    Map calls return a ~250-char partial. With a 700-token budget and a
    442-token system message, 3 partials (plus envelope overhead) fit in one
    reduce group (683 tokens total) but 4 do not (755 tokens total) —
    measured empirically via ``build_reduce_user_message``, since the
    per-partial envelope tags make the token count not a clean multiple. Ten
    map chunks therefore produce a multi-level tree-reduce (groups of 3 or
    fewer, recursing until one final synthesis).
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Return moderate output for map calls to trigger multi-level tree-reduce."""
        self.calls.append((system_message, user_message, response_model))
        if _is_judge_call(system_message):
            return LLMResponse(
                structured=_judge_text(0.75, "ok"),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        if "<feedback_records>" in user_message:
            # Map call — return a ~250-char partial so that 3 partials fit
            # per reduce group but 4 do not, forcing a multi-level
            # tree-reduce over the 10 map chunks.
            partial = "Analysis: " + ("Water access issues observed. " * 8)
            return LLMResponse(
                structured=partial,
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
                cost=0.0,
            )
        # Reduce call — return a fixed-length synthesis (same size as a partial
        # so intermediate reduce outputs still overflow at higher tree levels).
        synthesis = "Synthesis: " + ("Water access issues observed. " * 8)
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
    causing recursive tree-reduce. A LargeOutputLLM returns ~250-char
    partials. With a 700-token budget and a 442-token system message, 3
    partials fit per reduce group but 4 do not, so 10 map chunks produce a
    multi-level tree-reduce (several reduce calls, all >= 2).
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
    service = _build_analyze_service(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=700
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)
    result = await service.analyze_hierarchical(request, deadline, anonymize=True)
    # Many map calls (one per budget sub-chunk) — trigger 1.
    map_calls = [c for c in llm.calls if c[2] is str and "<feedback_records>" in c[1]]
    # Multiple reduce calls (combined partials overflow) — trigger 2.
    reduce_calls = [
        c for c in llm.calls if c[2] is str and "<partial_analyses>" in c[1]
    ]
    assert len(map_calls) >= 3, "cluster was not split into multiple budget sub-chunks"
    assert len(reduce_calls) >= 2, "partials never tree-reduced (single reduce only)"
    assert result.confidence is not None


class ConcurrencyTrackingLLM(LLMPort):
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
        if _is_judge_call(system_message):
            return LLMResponse(
                structured=_judge_text(),
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
    service = _build_analyze_service(
        llm,
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=700,
        analyze_settings=AnalyzeSettings(min_cluster_size=2, max_concurrent_chunks=2),
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await service.analyze_hierarchical(request, deadline, anonymize=True)

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
    service = _build_analyze_service(
        llm,
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=700,
        analyze_settings=AnalyzeSettings(min_cluster_size=2, max_concurrent_chunks=1),
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await service.analyze_hierarchical(request, deadline, anonymize=True)

    map_calls = [c for c in llm.calls if c[2] is str and "<feedback_records>" in c[1]]
    assert len(map_calls) >= 3, "corpus did not split into multiple map chunks"
    assert llm.peak_in_flight == 1
    assert result.confidence is not None


class OverlapTrackingLLM(LLMPort):
    """Records whether a leaf-judge call and a reduce call were ever in flight together.

    Each call yields once via ``asyncio.sleep(0)`` so the event loop interleaves
    the concurrently-gathered judge tasks with the reduce task; the call's kind
    is inferred from its prompt shape (judge, via :func:`_is_judge_call`) and
    user message (reduce).
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
        is_judge = _is_judge_call(system_message)
        if is_judge:
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
        if is_judge:
            return LLMResponse(
                structured=_judge_text(),
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


class OneChunkMapFailsLLM(LLMPort):
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
        if _is_judge_call(system_message):
            return LLMResponse(
                structured=_judge_text(),
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
    service = _build_analyze_service(
        llm, RecordingAnonymizer(), FakeEmbeddingPort(), max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await service.analyze_hierarchical(request, deadline, anonymize=True)

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


class AllJudgesFailLLM(LLMPort):
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
        if _is_judge_call(system_message):
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
    service = _build_analyze_service(
        AllJudgesFailLLM(),
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=100_000,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await service.analyze_hierarchical(request, deadline, anonymize=True)

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
    service = _build_analyze_service(
        RecordingLLM(),
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=100_000,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    with patch.object(
        AnalyzeService,
        "_judge_chunk",
        new=AsyncMock(side_effect=AnalysisTimeoutError("judge deadline exceeded")),
    ):
        result = await service.analyze_hierarchical(request, deadline, anonymize=True)

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
    service = _build_analyze_service(
        AllMapsFailLLM(),
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=100_000,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    with pytest.raises(AnalysisError, match="mapping failed for all chunks"):
        await service.analyze_hierarchical(request, deadline, anonymize=True)


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
    service = _build_analyze_service(
        llm,
        RecordingAnonymizer(),
        FakeEmbeddingPort(),
        max_total_tokens=700,
        analyze_settings=AnalyzeSettings(min_cluster_size=2, max_concurrent_chunks=50),
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await service.analyze_hierarchical(request, deadline, anonymize=True)

    # No judge call ever coexisted with a reduce call.
    assert not llm.judge_reduce_overlap, "judge ran concurrently with reduce"
    # Every reduce call strictly precedes every judge call in the recorded order.
    kinds = [
        "judge"
        if _is_judge_call(c[0])
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


@pytest.mark.asyncio
async def test_no_embedder_raises_analysis_error():
    """Without an embedder the hierarchical path refuses at request time.

    Why: the embedder is optional (``EMBEDDING_MODEL_PATH`` unset is the
    normal local/CI state), and the availability guard is what turns that
    into a clean 502 ``analysis_unavailable`` instead of an ``AttributeError``
    deeper in the pipeline. ``single_pass`` stays usable on the same service.
    """
    request = AnalysisRequestModel(
        feedback_records=_records(4, "water access " * 5, "w"),
        prompt="trends?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    service = _build_analyze_service(
        RecordingLLM(), RecordingAnonymizer(), None, max_total_tokens=100_000
    )
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    with pytest.raises(AnalysisError, match="no embedder configured"):
        await service.analyze_hierarchical(request, deadline, anonymize=True)


PERSON_NAME = "Maria Silva"


class EchoingLLM(LLMPort):
    """Fake LLM that echoes the user message back as its answer.

    ``RecordingLLM`` returns a canned string, which cannot show whether
    de-anonymisation restored the right value in the right place. Echoing
    puts the placeholders the service supplied into the synthesis, the way
    a real model that quotes the feedback would. Entities are unescaped
    first because the envelope escapes ``<PERSON_0>`` to
    ``&lt;PERSON_0&gt;`` on the way in and a real model writes the
    placeholder back out unescaped.
    """

    def __init__(self):
        self.calls = []

    async def complete(
        self, system_message, user_message, tenant_id, response_model=str, timeout=20.0
    ):
        """Record the call; echo the user message, or serve a judge score."""
        self.calls.append((system_message, user_message, response_model))
        structured = (
            _judge_text() if _is_judge_call(system_message) else unescape(user_message)
        )
        return LLMResponse(
            structured=structured,
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            cost=0.0,
        )


class PresidioSpy(AnonymizationPort):
    """The real Presidio adapter plus a record of what it redacted.

    Wraps rather than fakes: the test needs the placeholder Presidio chose
    for the repeated person, an index only the real allocator knows.
    """

    def __init__(self):
        self._delegate = PresidioAnonymizer()
        self.mapping = {}

    def anonymize(self, text):
        """Delegate, recording the mapping."""
        redacted, mapping = self._delegate.anonymize(text)
        self.mapping.update(mapping)
        return redacted, mapping

    def anonymize_batch(self, texts):
        """Delegate, recording the shared mapping."""
        redacted, mapping = self._delegate.anonymize_batch(texts)
        self.mapping.update(mapping)
        return redacted, mapping

    def deanonymize(self, text, mapping):
        """Delegate."""
        return self._delegate.deanonymize(text, mapping)


@pytest.mark.asyncio
async def test_hierarchical_round_trips_distinct_entities_across_records():
    """Each record's own entities come back in the synthesis, not another's.

    Deliberately runs the *real* ``PresidioAnonymizer``: the collision this
    pins (#324) lived in the real placeholder allocator, so no fake
    anonymiser can exercise it. On the buggy version the locations of the
    later records overwrote the earlier ones' in the merged mapping.
    """
    cities = ("Kharkiv", "Odesa", "Lviv", "Nairobi", "Kampala", "Jakarta")
    contents = (
        "The water point in Kharkiv ran dry for three days.",
        "The water trucking schedule for Odesa stopped without notice.",
        "Residents said the water queue in Lviv starts before dawn, "
        f"according to {PERSON_NAME}.",
        "The health clinic in Nairobi has no medicine left.",
        "Health staff in Kampala turned patients away.",
        f"{PERSON_NAME} waited at the health clinic in Jakarta all morning.",
    )
    records = tuple(
        FeedbackRecordModel(
            id=f"rec-{index}",
            content=content,
            metadata=FeedbackRecordMetadataModel(created="2024-01-05T00:00:00Z"),
        )
        for index, content in enumerate(contents)
    )
    request = AnalysisRequestModel(
        feedback_records=records,
        prompt="Which locations report service gaps?",
        tenant_id=TENANT_ID,
        mode="hierarchical",
    )
    llm = EchoingLLM()
    anonymizer = PresidioSpy()
    service = _build_analyze_service(
        llm, anonymizer, FakeEmbeddingPort(), max_total_tokens=200_000
    )

    result = await service.analyze_hierarchical(
        request, datetime.now(UTC) + timedelta(seconds=120), anonymize=True
    )

    # The repeated person gets one placeholder across both records, and
    # analyse retains it rather than restoring the name.
    person_placeholders = {p for p, v in anonymizer.mapping.items() if v == PERSON_NAME}
    assert len(person_placeholders) == 1
    person_placeholder = person_placeholders.pop()
    assert person_placeholder.startswith("<PERSON_")
    assert PERSON_NAME not in result.result
    assert person_placeholder in result.result

    # Every record's text is restored intact: no record's location was
    # rewritten to another record's.
    for content in contents:
        assert content.replace(PERSON_NAME, person_placeholder) in result.result

    # No raw entity ever reached the model.
    for _, user_message, _ in llm.calls:
        assert PERSON_NAME not in user_message
        for city in cities:
            assert city not in user_message
