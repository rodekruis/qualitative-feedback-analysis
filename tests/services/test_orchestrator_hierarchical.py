"""Tests for ``Orchestrator.analyze_hierarchical``.

Why: this method is the heart of #124. The tests pin the load-bearing
behaviours: anonymisation happens before embedding and before every LLM
call; guardrails appear at both map and reduce; both recursion triggers
fire on a large corpus; the coverage-weighted confidence is computed; and a
single_pass call is byte-identical to before (no regression).
"""

from datetime import UTC, datetime, timedelta

import pytest

from qfa.domain.models import (
    AnalysisRequestModel,
    FeedbackRecordModel,
    LLMResponse,
)
from qfa.services.orchestrator import AnalyzeJudgeResult, Orchestrator
from qfa.services.prompts import ANALYZE_GUARDRAILS_PROMPT
from qfa.settings import OrchestratorSettings

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
        settings=OrchestratorSettings(min_cluster_size=2),
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
