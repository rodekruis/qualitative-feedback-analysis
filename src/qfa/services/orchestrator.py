"""Orchestrator service — core business logic for feedback analysis.

Assembles prompts, enforces token limits, filters prompt injection,
manages retries with exponential backoff, and enforces deadlines.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qfa.domain.chunk_models import Chunk
from qfa.domain.clustering_models import CodingTrendTable
from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    FeedbackTooLargeError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import (
    AggregateSummaryResultModel,
    AnalysisRequestModel,
    AnalysisResultModel,
    AssignedCodeModel,
    CodedFeedbackRecordModel,
    CodingAssignmentRequestModel,
    CodingAssignmentResultModel,
    CodingNode,
    FeedbackRecordModel,
    FeedbackRecordSummaryModel,
    LLMResponse,
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityAnalysisResultModelList,
    SingleSummaryRequestModel,
    SummaryRequestModel,
    SummaryResultModel,
    T_Response,
)
from qfa.domain.ports import AnonymizationPort, EmbeddingPort, LLMPort
from qfa.domain.sensitivity_types import SENSITIVITY_TYPE_DESCRIPTIONS
from qfa.services.clustering import cluster_records
from qfa.services.coding_classifier import (
    JudgeResponse,
    build_judge_messages,
    build_pick_messages,
    parse_selected_indices,
)
from qfa.services.coding_trends import build_coding_trend_table
from qfa.services.hierarchical_prompts import (
    build_map_system_message,
    build_reduce_system_message,
    build_reduce_user_message,
)
from qfa.services.prompts import (
    ANALYZE_ACTION_PROMPT,
    ANALYZE_DISCLAIMER,
    ANALYZE_GUARDRAILS_PROMPT,
    ANALYZE_SYSTEM_PROMPT,
    JUDGE_UNAVAILABLE_EXPLANATION,
    build_analyze_judge_system_message,
    build_analyze_user_message,
    build_feedback_record_envelope,
    build_feedback_records_envelope,
    build_output_language_instruction,
)
from qfa.settings import (
    LLM_RETRY_BUDGET_MULTIPLIER,
    AnalyzeSettings,
    OrchestratorSettings,
)
from qfa.utils import timed

logger = logging.getLogger(__name__)

_SENSITIVITY_TYPE_GUIDANCE = "\n".join(
    f"- {sensitivity_type.value}: {description}"
    for sensitivity_type, description in SENSITIVITY_TYPE_DESCRIPTIONS.items()
)

_DEFAULT_SUMMARIZATION_PROMPT = (
    "Summarize the feedback item as concise bullet points.\n"
    "Strict Constraint: The summary must be extremely concise, using no more than 3-5 brief bullet points.\n"
    "Constraint: Each bullet point should be a single sentence fragment focusing only on the core sentiment or issue.\n"
    "Also create a short, 3-5 word descriptive title.\n"
    "Do not include markdown code fences.\n"
    "Use the same language as the input feedback item unless a target language is specified."
)

_DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT = (
    "You are an analytical assistant for a humanitarian organisation (Red Cross).\n"
    "You are given multiple feedback records from community members collected during humanitarian operations.\n"
    "Identify the key themes and issues raised across the feedback records.\n"
    "Order the bullet points from most to least frequently mentioned, so the most important problems are shown first.\n"
    "Each bullet point should name the theme and describe it as a concise sentence fragment.\n"
    "Scale the number of bullet points to the size and diversity of the input — use judgement.\n"
    "Also create a short, 3-5 word descriptive title reflecting the dominant theme.\n"
    "Do not include markdown code fences.\n"
    "Use the same language as the input feedback records unless a target language is specified."
)

_DEFAULT_SENSITIVITY_DETECTION_PROMPT = (
    "Analyze each feedback record and detect whether it contains sensitive content.\n"
    "Classify sensitivity using only the SensitivityType enum values from the response schema.\n"
    "For each record, include a concise natural-language explanation for the classification.\n"
    f"SensitivityType guidance:\n{_SENSITIVITY_TYPE_GUIDANCE}\n"
    "Return one result per input record with the matching feedback_record_id.\n"
    "If no sensitive content is present, return an empty sensitivity_types tuple for that record.\n"
    "Do not include markdown code fences.\n"
    "Note that anonymization might have taken place (e.g. ``<PERSON_0>``, ``<LOCATION_1>``). \n"
    "Please act as if these were not anonymized. For example, if you see ``<PERSON_0>``"
    " treat it as if it said 'John Doe' and classify sensitivity accordingly. \n"
    "Please note that we prefer false positives over false negatives in this classification."
)

_JUDGE_PROMPT = """
You are evaluating the quality of a summary.

Source text:
---
{source_text}
---

Summary:
---
{summary}
---

Score the summary using three criteria. Each must be a float between 0 and 1.

Faithfulness:
1.0 = fully supported by source, no hallucinations
0.5 = mostly correct, minor issues
0.0 = major inaccuracies

Coverage:
1.0 = includes all key points
0.5 = partially covers key points
0.0 = misses most important points

Clarity:
1.0 = very clear and concise
0.5 = somewhat clear
0.0 = confusing or poorly written

Compute the final score as:
quality_score = 0.6 * faithfulness + 0.3 * coverage + 0.1 * clarity

Output rules:
- Return ONLY the final quality_score
- Return a single float between 0 and 1
- No JSON
- No explanation
- No extra text
- Example output: 0.82
"""

#: Minimum time (seconds) required for an LLM attempt to be viable.
_MINIMUM_ATTEMPT_WINDOW = 10.0

_JUDGE_USER_MESSAGE = "."

_AlignedItemT = TypeVar("_AlignedItemT")


class AnalyzeJudgeResult(BaseModel):
    """Structured output of the analyse-judge LLM call.

    The judge returns both a quality score in [0,1] and a short
    natural-language ``uncertainty_explanation`` the analyst can read to
    understand why the score is what it is.
    """

    model_config = ConfigDict(frozen=True)

    quality_score: float = Field(ge=0.0, le=1.0)
    uncertainty_explanation: str = Field(min_length=1)


def _parse_judge_quality_score(raw: str) -> float:
    """Parse a single float on the first line of the judge model output."""
    line = raw.strip().split("\n", maxsplit=1)[0].strip()
    try:
        score = float(line)
    except ValueError as exc:
        raise AnalysisError("LLM judge returned invalid quality score") from exc
    if not 0.0 <= score <= 1.0:
        raise AnalysisError("LLM judge returned quality score outside 0.0-1.0")
    return score


def _build_judge_system_message(source_text: str, summary: str) -> str:
    """Fill the judge prompt with the provided source text and summary."""
    return _JUDGE_PROMPT.format(source_text=source_text, summary=summary)


@dataclass
class _ScoredCode:
    coding_level_1_id: str
    coding_level_1_name: str
    coding_level_2_id: str
    coding_level_2_name: str
    coding_level_3_id: str
    coding_level_3_name: str
    confidence_level_1: float
    confidence_level_2: float
    confidence_level_3: float
    explanation_level_1: str
    explanation_level_2: str
    explanation_level_3: str

    @property
    def confidence_aggregate(self) -> float:
        return min(
            self.confidence_level_1, self.confidence_level_2, self.confidence_level_3
        )

    @property
    def explanation(self) -> str:
        return (
            f"Level 1 ({self.confidence_level_1:.2f}): {self.explanation_level_1} "
            f"Level 2 ({self.confidence_level_2:.2f}): {self.explanation_level_2} "
            f"Level 3 ({self.confidence_level_3:.2f}): {self.explanation_level_3}"
        )


@dataclass
class _SlotTiming:
    """Split timing for one semaphore-bounded hierarchical LLM call.

    ``queued_seconds`` is the time spent waiting to acquire the concurrency
    semaphore; ``call_seconds`` is the LLM completion itself, measured only
    *after* the slot was acquired. Keeping them apart makes the per-chunk
    debug lines honest: a single combined duration folds queue-wait into
    "call time", so a 5s call that waited 100s in the queue looked like a
    110s call (and like a near-timeout when it was nothing of the sort).
    """

    queued_seconds: float = 0.0
    call_seconds: float = 0.0


class Orchestrator:
    """Core orchestration service for feedback analysis.

    Assembles prompts from feedback records, validates input,
    calls the LLM through the ``LLMPort``, and manages retries
    with exponential backoff and deadline enforcement.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter.
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before LLM calls.
    settings : OrchestratorSettings
        Cross-cutting orchestrator configuration (retry policy, token
        budget estimation, metadata allow-list).
    llm_timeout_seconds : float
        Maximum time in seconds for a single LLM call.
    max_total_tokens : int
        Maximum estimated total tokens for a single request.
    analyze_settings : AnalyzeSettings | None
        Configuration for the ``POST /v1/analyze`` endpoint (clustering
        knobs, coding-trend table inputs, default period). Defaults to
        :class:`AnalyzeSettings` with environment-loaded values so tests
        and callers that don't care about analyze tuning can omit it.
    embedder : EmbeddingPort | None
        Optional embedder for ``mode=hierarchical``. ``None`` makes the
        hierarchical path raise :class:`AnalysisError` at request time.
    """

    # Entity types whose placeholders are NOT restored in `analyze` output.
    # Defense in depth for the "do not identify individuals" guardrail in
    # `ANALYZE_GUARDRAILS_PROMPT`: even if the analyse LLM echoes a
    # placeholder we supplied, the analyst never sees the underlying name.
    # Scoped to `analyze` only — `summarize`/`assign_codes` still restore
    # all placeholders because their per-record output is meant to be
    # faithful to the source.
    _ANALYZE_RETAINED_PLACEHOLDER_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"PERSON"}
    )

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        settings: OrchestratorSettings,
        llm_timeout_seconds: float,
        max_total_tokens: int,
        analyze_settings: AnalyzeSettings | None = None,
        embedder: EmbeddingPort | None = None,
    ) -> None:
        self._llm = llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._embedder = embedder
        self._settings = settings
        # AnalyzeSettings is endpoint-scoped; default-construct when callers
        # (mostly tests) don't supply one so environment-driven knobs still
        # apply without forcing every Orchestrator construction site to thread
        # the extra argument.
        self._analyze_settings = analyze_settings or AnalyzeSettings()
        self._llm_timeout_seconds = llm_timeout_seconds
        self._max_total_tokens = max_total_tokens

    @classmethod
    def _is_retained_analyze_placeholder(cls, placeholder: str) -> bool:
        """Return True when ``placeholder`` belongs to a retained entity type.

        Placeholders use the form ``<ENTITY_TYPE_N>`` (e.g. ``<PERSON_0>``,
        ``<LOCATION_3>``), so a prefix match on ``<TYPE_`` correctly handles
        any index Presidio chooses.
        """
        return any(
            placeholder.startswith(f"<{entity_type}_")
            for entity_type in cls._ANALYZE_RETAINED_PLACEHOLDER_TYPES
        )

    async def analyze_bulk(
        self,
        request: AnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResultModel:
        """Analyze a batch of feedback records.

        Two LLM calls are issued: the analysis itself, then a judge call
        that produces ``quality_score`` and ``uncertainty_explanation``.

        Also computes the deterministic ``coding_trends`` table from
        record metadata (no LLM, no chunking) and returns it. The table
        is a free win for the single-call path: it depends only on
        metadata and date parsing, not on map-reduce. When metadata is
        absent the field comes back as ``None`` rather than failing.

        Edge cases
        ----------
        - ``mode`` other than ``"single_pass"`` → 422.
        - Judge call failure → 200 with ``quality_score=null`` and the
          constant unavailable-judge explanation.
        - Estimated tokens above the cap → 413 ``payload_too_large``;
          reduce the batch size. Hierarchical / map-reduce is tracked in #124.
        - Existing regex prompt-injection tripwire still applies and
          returns 422 ``prompt_injection_detected``.
        """
        system_message = (
            f"{ANALYZE_SYSTEM_PROMPT}\n\n"
            f"{ANALYZE_GUARDRAILS_PROMPT}\n\n"
            f"{ANALYZE_ACTION_PROMPT}"
            f"{build_output_language_instruction(request.output_language)}"
        )
        user_message = build_analyze_user_message(
            request.prompt, request.feedback_records
        )

        anonymized_user_message = user_message
        anonymization_mapping: dict[str, str] = {}
        anonymized_prompt = request.prompt
        if anonymize:
            anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
                user_message
            )
            anonymized_prompt, _ = self._anonymizer.anonymize(request.prompt)

        analyse_timeout = self._check_deadline_and_get_timeout(deadline)
        analyse_response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=analyse_timeout,
        )
        analysis_text: str = analyse_response.structured

        if anonymize:
            restorable_mapping = {
                placeholder: original
                for placeholder, original in anonymization_mapping.items()
                if not self._is_retained_analyze_placeholder(placeholder)
            }
            analysis_text = self._anonymizer.deanonymize(
                analysis_text, restorable_mapping
            )

        quality_score: float | None
        uncertainty_explanation: str
        try:
            judge_timeout = self._check_deadline_and_get_timeout(deadline)
            judge_system = build_analyze_judge_system_message(
                source_text=anonymized_user_message,
                analyst_prompt=anonymized_prompt,
                analysis=analyse_response.structured,
            )
            judge_response = await self._llm.complete(
                system_message=judge_system,
                user_message=_JUDGE_USER_MESSAGE,
                tenant_id=request.tenant_id,
                response_model=AnalyzeJudgeResult,
                timeout=judge_timeout,
            )
            quality_score = judge_response.structured.quality_score
            uncertainty_explanation = judge_response.structured.uncertainty_explanation
        except (
            LLMError,
            LLMTimeoutError,
            LLMRateLimitError,
            ValidationError,
            AnalysisError,
        ) as exc:
            logger.warning(
                "Analyse judge call failed: error_class=%s",
                type(exc).__name__,
            )
            quality_score = None
            uncertainty_explanation = JUDGE_UNAVAILABLE_EXPLANATION

        # Deterministic, non-LLM coding-trend table from ORIGINAL metadata
        # (metadata is not anonymised; codes/dates are not PII). Built for
        # single_pass too — it depends only on the input metadata, not on the
        # chunking/map-reduce pipeline.
        trend_table = build_coding_trend_table(
            request.feedback_records,
            date_field=self._analyze_settings.coding_trend_date_field,
            code_fields=self._analyze_settings.coding_trend_code_fields,
            period=(
                request.period or self._analyze_settings.default_coding_trend_period
            ),
        )

        return AnalysisResultModel(
            result=f"{ANALYZE_DISCLAIMER}{analysis_text}",
            quality_score=quality_score,
            uncertainty_explanation=uncertainty_explanation,
            coding_trends=trend_table,
        )

    async def analyze_hierarchical(
        self,
        request: AnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResultModel:
        """Analyse a corpus larger than the single-call token cap.

        Flow: anonymise each record → deterministic coding-trend table →
        embed record texts (synchronous, CPU-bound) → cluster (HDBSCAN) →
        MAP each chunk to a partial (leaf LLM call) → REDUCE the partials
        (with the trend table), recursing when a chunk or the partial set
        overflows the token budget → leaf-JUDGE each partial for the
        confidence. Reduce runs before the judges: the synthesis is the
        deliverable and gets slot priority, while the judges only feed the
        secondary confidence signal. The returned ``confidence`` is the
        coverage-weighted mean of the per-chunk judge scores, computed over
        only the chunks that were successfully judged — chunks whose map or
        judge call failed are excluded (not scored 0.0) and their count is
        reported in ``uncertainty_explanation``. ``confidence`` is ``None``
        when no chunk could be judged.

        Anonymisation happens before embedding and before every LLM call.
        Guardrails are applied at both the map and reduce prompts.

        Raises
        ------
        AnalysisError
            When no embedder is configured or the corpus cannot be analysed.
        """
        if self._embedder is None:
            raise AnalysisError(
                "Hierarchical analysis is not available: no embedder configured"
            )

        logger.info(
            "analyze_hierarchical start: %d record(s) tenant=%s anonymize=%s",
            len(request.feedback_records),
            request.tenant_id,
            anonymize,
        )

        # 1. Anonymise each record's text up front (before embed + LLM).
        logger.info(
            "Starting anonymization of %d records...", len(request.feedback_records)
        )
        with timed() as anonymize_sw:
            anonymized_records, mapping = self._anonymize_records(
                request.feedback_records, anonymize
            )
            anonymized_prompt = request.prompt
            if anonymize:
                # Single pass over the prompt, capturing both the redacted
                # text and its mapping (previously this ran Presidio twice —
                # once for the mapping, once for the text).
                anonymized_prompt, prompt_map = self._anonymizer.anonymize(
                    request.prompt
                )
                mapping = {**mapping, **prompt_map}
        logger.info(
            "anonymisation: %d record(s) in %.2fs",
            len(request.feedback_records),
            anonymize_sw.elapsed_seconds,
        )

        # 2. Deterministic coding-trend table from ORIGINAL metadata
        #    (metadata is not anonymised; codes/dates are not PII).
        trend_table = build_coding_trend_table(
            request.feedback_records,
            date_field=self._analyze_settings.coding_trend_date_field,
            code_fields=self._analyze_settings.coding_trend_code_fields,
            period=(
                request.period or self._analyze_settings.default_coding_trend_period
            ),
        )

        # 3. Embed (synchronous, CPU-bound) then cluster into budget chunks.
        texts = tuple(r.content for r in anonymized_records)
        logger.info("starting embedding of %d record(s)", len(texts))
        with timed() as embed_sw:
            vectors = self._embedder.embed(texts)
        logger.info(
            "embedding: %d record(s) in %.2fs", len(texts), embed_sw.elapsed_seconds
        )

        logger.info("starting clustering of %d record(s)", len(texts))
        with timed() as cluster_sw:
            chunks = cluster_records(
                records=anonymized_records,
                vectors=vectors,
                min_cluster_size=self._analyze_settings.min_cluster_size,
                max_total_tokens=self._max_total_tokens,
                chars_per_token=self._settings.chars_per_token,
                metric=self._analyze_settings.clustering_metric,
                target_chunk_tokens=self._analyze_settings.target_chunk_tokens,
                date_field=self._analyze_settings.coding_trend_date_field,
            )
        logger.info(
            "clustering: %d record(s) -> %d chunk(s) in %.2fs",
            len(texts),
            len(chunks),
            cluster_sw.elapsed_seconds,
        )

        # One semaphore bounds *every* hierarchical LLM call (map, leaf judge,
        # reduce) to ``max_concurrent_chunks``, so total concurrency stays
        # capped across all phases. cap=1 therefore remains fully sequential.
        max_in_flight = self._analyze_settings.max_concurrent_chunks
        semaphore = asyncio.Semaphore(max_in_flight)

        # 4. MAP: produce one partial per chunk, concurrently. Only the partials
        #    are on the critical path to REDUCE; the leaf-judge scores feed only
        #    the final confidence, so judging is deferred to phase 5, which runs
        #    REDUCE first and then the judges (see that block for why).
        #    ``asyncio.gather`` preserves chunk order, so partials and
        #    chunk_sizes stay aligned with ``chunks``.
        logger.info(
            "starting map phase: %d chunk(s), up to %d concurrent LLM call(s)",
            len(chunks),
            max_in_flight,
        )

        async def _map_one(index: int, chunk: Chunk) -> str:
            """Produce one chunk's partial (the judge runs separately)."""
            logger.debug(
                "starting map chunk %d/%d: %d record(s)",
                index,
                len(chunks),
                len(chunk.records),
            )
            timing = _SlotTiming()
            with timed() as chunk_sw:
                partial = await self._map_chunk(
                    anonymized_prompt,
                    chunk.records,
                    request.tenant_id,
                    deadline,
                    semaphore,
                    timing=timing,
                )
            logger.debug(
                "map chunk %d/%d done in %.2fs (queued=%.2fs call=%.2fs)",
                index,
                len(chunks),
                chunk_sw.elapsed_seconds,
                timing.queued_seconds,
                timing.call_seconds,
            )
            return partial

        with timed() as map_sw:
            partials_with_exceptions: list[str | BaseException] = list(
                await asyncio.gather(
                    *(_map_one(i, chunk) for i, chunk in enumerate(chunks, start=1)),
                    return_exceptions=True,
                )
            )
            errors: list[BaseException] = []
            partials: list[str | None] = []
            for partial_or_exc in partials_with_exceptions:
                if isinstance(partial_or_exc, BaseException):
                    errors.append(partial_or_exc)
                    partials.append(None)
                else:
                    partials.append(partial_or_exc)
            # check if any errors occurred and log them.
            # Iff ALL chunks failed, raise.
            if errors:
                if len(errors) == len(chunks):
                    raise AnalysisError("mapping failed for all chunks")
                else:
                    error_classes = sorted({type(exc).__name__ for exc in errors})
                    logger.warning(
                        "Errors mapping %d/%d chunks: error_classes=%s",
                        len(errors),
                        len(chunks),
                        ",".join(error_classes),
                    )

        chunk_sizes: list[int] = [len(chunk.records) for chunk in chunks]
        logger.info(
            "map phase: %d chunk(s) in %.2fs", len(chunks), map_sw.elapsed_seconds
        )

        # 5. REDUCE first, then JUDGE. The synthesis is the deliverable, so it
        #    gets first claim on the semaphore slots and short-circuits the
        #    judges on failure (a reduce error propagates and we never spend
        #    tokens judging a synthesis we're about to discard). The leaf judges
        #    only feed the secondary ``confidence`` signal, so they run after and
        #    absorb whatever deadline pressure remains. Sequencing costs little
        #    wall-clock: judge and reduce share one semaphore, so they were
        #    already time-slicing the same ``max_concurrent_chunks`` slots.
        async def _judge_all() -> list[float | None]:
            async def _judge_one(
                index: int, chunk: Chunk, partial: Optional[str]
            ) -> float | None:
                logger.debug("starting judge chunk %d/%d", index, len(chunks))
                timing = _SlotTiming()
                with timed() as judge_sw:
                    score = await self._judge_chunk(
                        anonymized_prompt,
                        chunk.records,
                        partial,
                        request.tenant_id,
                        deadline,
                        semaphore,
                        timing=timing,
                    )
                logger.debug(
                    "judge chunk %d/%d done: judge=%s in %.2fs "
                    "(queued=%.2fs call=%.2fs)",
                    index,
                    len(chunks),
                    f"{score:.2f}" if score is not None else "excluded",
                    judge_sw.elapsed_seconds,
                    timing.queued_seconds,
                    timing.call_seconds,
                )
                return score

            return list(
                await asyncio.gather(
                    *(
                        _judge_one(i, chunk, partial)
                        for i, (chunk, partial) in enumerate(
                            zip(chunks, partials, strict=True), start=1
                        )
                    ),
                )
            )

        async def _reduce() -> str:
            # Drop chunks whose map call failed (None). They contribute nothing
            # to the synthesis, and passing None into build_reduce_user_message
            # would raise (escape_for_tag_envelope expects a str). Such chunks
            # are *excluded* from the confidence too (see _judge_chunk → None),
            # rather than scored 0.0 — a dropped chunk is unverified, not
            # unfaithful. Their absence is surfaced in uncertainty_explanation.
            successful_partials = tuple(p for p in partials if p is not None)
            logger.info(
                "starting reduce phase over %d partial(s)", len(successful_partials)
            )
            return await self._reduce_partials(
                anonymized_prompt,
                successful_partials,
                trend_table,
                request.tenant_id,
                deadline,
                semaphore,
                request.output_language,
            )

        with timed() as reduce_sw:
            synthesis = await _reduce()
        logger.info("reduce phase in %.2fs", reduce_sw.elapsed_seconds)

        logger.info("starting judge phase")
        with timed() as judge_sw:
            try:
                chunk_scores = await _judge_all()
            except (LLMTimeoutError, AnalysisTimeoutError) as exc:
                # The synthesis (the deliverable) is already produced; a judge
                # phase that runs out of time must NOT discard it. Per-chunk
                # judges already swallow these into None, so this is a phase-level
                # backstop: treat every chunk as unjudged (confidence -> None) and
                # fall through to the fast, pure-Python result assembly below.
                logger.warning(
                    "Judge phase aborted (%s); returning the synthesis with "
                    "confidence unavailable.",
                    type(exc).__name__,
                )
                chunk_scores = [None] * len(chunks)
        logger.info("judge phase in %.2fs", judge_sw.elapsed_seconds)

        # 6. Aggregate per-chunk faithfulness into one confidence. Chunks whose
        #    judge timed out or errored (None) are EXCLUDED — they neither count
        #    toward nor against the mean, so a time-starved judge cannot masquerade
        #    as a faithfulness of 0.0. Confidence is None when nothing was judged.
        judged = [
            (score, weight)
            for score, weight in zip(chunk_scores, chunk_sizes, strict=True)
            if score is not None
        ]
        excluded = len(chunks) - len(judged)
        confidence: float | None
        if not judged:
            confidence = None
            uncertainty = (
                f"Confidence unavailable: none of the {len(chunks)} chunk(s) "
                f"could be leaf-judged (all judge calls failed or timed out)."
            )
        else:
            judged_scores = [score for score, _ in judged]
            judged_weights = [weight for _, weight in judged]
            confidence = self._coverage_weighted_mean(judged_scores, judged_weights)
            floor = min(judged_scores)
            excluded_note = (
                f" ({excluded} chunk(s) excluded: judge failed or timed out)"
                if excluded
                else ""
            )
            uncertainty = (
                f"Leaf-judged confidence is a coverage-weighted mean over "
                f"{len(judged)} of {len(chunks)} chunk(s){excluded_note}; the "
                f"lowest single-chunk faithfulness was {floor:.2f}."
            )

        # 7. De-anonymise the synthesis (retain PERSON placeholders as in `analyze`).
        analysis_text = synthesis
        if anonymize:
            restorable = {
                placeholder: original
                for placeholder, original in mapping.items()
                if not self._is_retained_analyze_placeholder(placeholder)
            }
            analysis_text = self._anonymizer.deanonymize(analysis_text, restorable)

        # One-line breakdown so a single log line answers "where did the time
        # go?" without scrolling. The total is the sum of the timed phases
        # (de-anonymisation and trend-table building are sub-millisecond).
        logger.info(
            "analyze_hierarchical done in %.2fs "
            "(anonymise=%.2fs embed=%.2fs cluster=%.2fs map=%.2fs "
            "reduce=%.2fs judge=%.2fs)",
            anonymize_sw.elapsed_seconds
            + embed_sw.elapsed_seconds
            + cluster_sw.elapsed_seconds
            + map_sw.elapsed_seconds
            + reduce_sw.elapsed_seconds
            + judge_sw.elapsed_seconds,
            anonymize_sw.elapsed_seconds,
            embed_sw.elapsed_seconds,
            cluster_sw.elapsed_seconds,
            map_sw.elapsed_seconds,
            reduce_sw.elapsed_seconds,
            judge_sw.elapsed_seconds,
        )

        return AnalysisResultModel(
            result=f"{ANALYZE_DISCLAIMER}{analysis_text}",
            confidence=confidence,
            uncertainty_explanation=uncertainty,
            coding_trends=trend_table,
        )

    def _anonymize_records(
        self,
        records: tuple[FeedbackRecordModel, ...],
        anonymize: bool,
    ) -> tuple[tuple[FeedbackRecordModel, ...], dict[str, str]]:
        """Anonymise each record's text, returning new records + merged mapping.

        Metadata is left untouched (codes/dates are not PII and feed the
        deterministic trend table). When ``anonymize`` is False, records are
        returned unchanged with an empty mapping.
        """
        if not anonymize:
            return records, {}
        merged: dict[str, str] = {}
        new_records: list[FeedbackRecordModel] = []
        for record in records:
            redacted, mapping = self._anonymizer.anonymize(record.content)
            merged.update(mapping)
            new_records.append(record.model_copy(update={"content": redacted}))
        return tuple(new_records), merged

    async def _bounded_complete(
        self,
        semaphore: asyncio.Semaphore,
        *,
        system_message: str,
        user_message: str,
        tenant_id: str,
        response_model: type[T_Response],
        deadline: datetime,
        timing: _SlotTiming | None = None,
    ) -> LLMResponse[T_Response]:
        """Run one LLM completion, bounded by ``semaphore`` and the deadline.

        ``semaphore`` caps how many completions run at once across the whole
        hierarchical pipeline (map, leaf judge, reduce), so concurrency stays
        within ``max_concurrent_chunks`` across every phase. The
        deadline/timeout is computed *after* acquiring a slot, so a
        completion that queued behind others still honours the remaining budget
        (and raises ``AnalysisTimeoutError`` if the deadline passed while it
        waited).

        When ``timing`` is supplied it is populated with the queue-wait and the
        post-acquire call duration as two separate fields, so callers can log
        them apart rather than reporting one combined number that hides how long
        the call sat waiting for a slot.
        """
        queue_start = time.perf_counter()
        async with semaphore:
            acquired_at = time.perf_counter()
            if timing is not None:
                timing.queued_seconds = acquired_at - queue_start
            # Compute the timeout only now: queue-wait already elapsed, so the
            # per-call window reflects the budget that actually remains.
            timeout = self._check_deadline_and_get_timeout(deadline)
            try:
                return await self._llm.complete(
                    system_message=system_message,
                    user_message=user_message,
                    tenant_id=tenant_id,
                    response_model=response_model,
                    timeout=timeout,
                )
            finally:
                if timing is not None:
                    timing.call_seconds = time.perf_counter() - acquired_at

    async def _map_chunk(
        self,
        analyst_prompt: str,
        records: tuple[FeedbackRecordModel, ...],
        tenant_id: str,
        deadline: datetime,
        semaphore: asyncio.Semaphore,
        timing: _SlotTiming | None = None,
    ) -> str:
        """Produce one partial analysis for a chunk (no judging).

        The leaf judge that scores this partial runs as a separate phase
        (see :meth:`_judge_chunk`), after reduce — both depend only on the
        partials, and deferring the judges keeps a time-starved judge phase
        from discarding the already-produced synthesis.
        """
        response = await self._bounded_complete(
            semaphore,
            system_message=build_map_system_message(),
            user_message=build_analyze_user_message(analyst_prompt, records),
            tenant_id=tenant_id,
            response_model=str,
            deadline=deadline,
            timing=timing,
        )
        return response.structured

    async def _judge_chunk(
        self,
        analyst_prompt: str,
        records: tuple[FeedbackRecordModel, ...],
        partial: Optional[str],
        tenant_id: str,
        deadline: datetime,
        semaphore: asyncio.Semaphore,
        timing: _SlotTiming | None = None,
    ) -> float | None:
        """Leaf-judge a partial against its own (anonymised) chunk.

        Returns the faithfulness score in ``[0, 1]``, or ``None`` when the chunk
        cannot be judged — either its map call failed (``partial is None``) or
        the judge call itself failed/timed out. ``None`` means *excluded* from
        the confidence aggregation (unverified ≠ unfaithful), not scored 0.0, so
        a time-starved judge does not depress the reported confidence.
        """
        if partial is None:
            # The map call for this chunk failed; there is nothing to judge.
            return None
        user_message = build_analyze_user_message(analyst_prompt, records)
        try:
            judge_system = build_analyze_judge_system_message(
                source_text=user_message,
                analyst_prompt=analyst_prompt,
                analysis=partial,
            )
            judge_response = await self._bounded_complete(
                semaphore,
                system_message=judge_system,
                user_message=_JUDGE_USER_MESSAGE,
                tenant_id=tenant_id,
                response_model=AnalyzeJudgeResult,
                deadline=deadline,
                timing=timing,
            )
            return judge_response.structured.quality_score
        except (
            LLMError,
            LLMTimeoutError,
            LLMRateLimitError,
            ValidationError,
            AnalysisError,
        ) as exc:
            logger.warning(
                "Hierarchical leaf judge failed: error_class=%s", type(exc).__name__
            )
            return None

    async def _reduce_partials(
        self,
        analyst_prompt: str,
        partials: tuple[str, ...],
        trend_table: CodingTrendTable | None,
        tenant_id: str,
        deadline: datetime,
        semaphore: asyncio.Semaphore,
        output_language: str | None = None,
    ) -> str:
        """Synthesise partials into one analysis, tree-reducing on overflow.

        ``semaphore`` bounds the reduce LLM calls together with the concurrently
        running leaf judges, so total pipeline concurrency stays within
        ``max_concurrent_chunks``.

        If the reduce user message would exceed the token budget, the
        partials are split into budget-sized groups, each reduced to an
        intermediate synthesis, and the reduce is applied again over those
        intermediates (recursion trigger 2). The trend table is attached to
        the FINAL reduce only (intermediates pass ``None``) so it anchors
        the top-level synthesis without being double-counted.

        Convergence guarantee: when all groups are singletons and the set of
        intermediates has the same length as the input partials (no progress),
        we emit a single LLM call on the partials anyway so the recursion
        always terminates.
        """
        system_message = build_reduce_system_message(output_language)

        def _fits(items: tuple[str, ...], table: CodingTrendTable | None) -> bool:
            user = build_reduce_user_message(
                analyst_prompt=analyst_prompt,
                partial_analyses=items,
                trend_table=table,
            )
            return (
                len(system_message + user) // self._settings.chars_per_token
                <= self._max_total_tokens
            )

        async def _reduce_once(items: tuple[str, ...]) -> str:
            """Synthesise ``items`` (with the trend table) in one reduce call."""
            response = await self._bounded_complete(
                semaphore,
                system_message=system_message,
                user_message=build_reduce_user_message(
                    analyst_prompt=analyst_prompt,
                    partial_analyses=items,
                    trend_table=trend_table,
                ),
                tenant_id=tenant_id,
                response_model=str,
                deadline=deadline,
            )
            return response.structured

        # Base case: everything fits in one reduce call, or only one partial remains.
        if len(partials) <= 1 or _fits(partials, trend_table):
            return await _reduce_once(partials)

        # Recursive case: group partials to budget, reduce each group, recurse.
        groups = self._group_partials_to_budget(
            analyst_prompt, system_message, partials
        )

        # Convergence safeguard: if grouping produced all singleton groups
        # (every partial overflows on its own), we cannot shrink the partial
        # count further. Emit one reduce call over all partials to terminate.
        if all(len(g) == 1 for g in groups) and len(groups) == len(partials):
            return await _reduce_once(partials)

        logger.debug(
            "reduce: %d partial(s) exceed the token budget; tree-reducing in "
            "%d group(s)",
            len(partials),
            len(groups),
        )
        intermediates = [
            await self._reduce_partials(
                analyst_prompt,
                group,
                None,
                tenant_id,
                deadline,
                semaphore,
                output_language,
            )
            for group in groups
        ]
        return await self._reduce_partials(
            analyst_prompt,
            tuple(intermediates),
            trend_table,
            tenant_id,
            deadline,
            semaphore,
            output_language,
        )

    def _group_partials_to_budget(
        self,
        analyst_prompt: str,
        system_message: str,
        partials: tuple[str, ...],
    ) -> list[tuple[str, ...]]:
        """Greedily pack partials into groups whose reduce prompt fits the budget.

        Guarantees progress: a single partial that alone overflows still
        occupies its own group (the next reduce layer will summarise it,
        shrinking it). Produces at least two groups when more than one
        partial is given (so recursion strictly reduces the count).
        """
        budget = self._max_total_tokens
        groups: list[tuple[str, ...]] = []
        current: list[str] = []
        for partial in partials:
            candidate = (*current, partial)
            user = build_reduce_user_message(
                analyst_prompt=analyst_prompt,
                partial_analyses=candidate,
                trend_table=None,
            )
            fits = (
                len(system_message + user) // self._settings.chars_per_token <= budget
            )
            if current and not fits:
                groups.append(tuple(current))
                current = [partial]
            else:
                current.append(partial)
        if current:
            groups.append(tuple(current))
        # Ensure the recursion shrinks the partial count (avoid 1 group == input).
        if len(groups) == 1 and len(partials) > 1:
            mid = len(partials) // 2
            groups = [partials[:mid], partials[mid:]]
        return groups

    @staticmethod
    def _coverage_weighted_mean(scores: list[float], weights: list[int]) -> float:
        """Coverage-weighted mean of leaf scores (weighted by chunk record count).

        Returns 0.0 for an empty input. Each chunk's faithfulness is weighted
        by how many records it covers, so a large chunk influences the
        confidence more than a tiny outlier chunk.
        """
        total_weight = sum(weights)
        if total_weight == 0:
            return 0.0
        return sum(s * w for s, w in zip(scores, weights, strict=True)) / total_weight

    async def summarize_bulk(
        self,
        request: SummaryRequestModel,
        deadline: datetime,
    ) -> AggregateSummaryResultModel:
        """Summarize multiple feedback records as a single aggregate summary.

        Parameters
        ----------
        request : SummaryRequest
            The summarization request containing feedback records and options.
        deadline : datetime
            Absolute UTC deadline by which summarization must complete.

        Returns
        -------
        AggregateSummaryResult
            A single aggregate summary with themes ordered by frequency.
        """
        system_message = _DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT
        system_message += build_output_language_instruction(
            request.output_language, subject="title and summary"
        )
        if request.prompt:
            system_message += f"\nAdditional instructions: {request.prompt}"

        user_message = build_feedback_records_envelope(
            request.feedback_records, include_metadata=False
        )

        anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
            user_message
        )

        timeout = self._check_deadline_and_get_timeout(deadline)
        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=AggregateSummaryResultModel,
            timeout=timeout,
        )
        total_cost = response.cost

        judge_system = _build_judge_system_message(
            anonymized_user_message, response.structured.summary
        )

        judge_timeout = self._check_deadline_and_get_timeout(deadline)
        judge_response = await self._llm.complete(
            system_message=judge_system,
            user_message=_JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        total_cost += judge_response.cost
        quality_score = _parse_judge_quality_score(judge_response.structured)

        response.structured.quality_score = quality_score
        response.structured.ids = tuple(
            record.id for record in request.feedback_records
        )

        return_model_as_string = response.structured.model_dump_json()
        unanonymized_return_model_as_string = self._anonymizer.deanonymize(
            return_model_as_string, anonymization_mapping
        )
        return AggregateSummaryResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )

    async def summarize(
        self,
        request: SingleSummaryRequestModel,
        deadline: datetime,
    ) -> FeedbackRecordSummaryModel:
        """Summarize a single feedback record.

        Parameters
        ----------
        request : SingleSummaryRequestModel
            The summarization request containing a single feedback record.
        deadline : datetime
            Absolute UTC deadline by which summarization must complete.

        Returns
        -------
        FeedbackRecordSummaryModel
            The summary title and content for the feedback record.

        Raises
        ------
        AnalysisError
            When the LLM returns invalid output or another non-recoverable
            error occurs.
        """
        timeout = self._check_deadline_and_get_timeout(deadline)
        system_message = _DEFAULT_SUMMARIZATION_PROMPT

        user_message = build_feedback_record_envelope(
            request.feedback_record, include_metadata=False
        )
        anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
            user_message
        )

        llm_completion = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=SummaryResultModel,
            timeout=timeout,
        )

        if not llm_completion.structured.feedback_record_summaries:
            raise AnalysisError("LLM returned no summaries for the feedback record.")

        judge_system = _build_judge_system_message(
            anonymized_user_message,
            llm_completion.structured.feedback_record_summaries[0].summary,
        )
        judge_timeout = self._check_deadline_and_get_timeout(deadline)
        judge_response = await self._llm.complete(
            system_message=judge_system,
            user_message=_JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        quality_score = _parse_judge_quality_score(judge_response.structured)

        return_model_as_string = llm_completion.structured.model_dump_json()
        unanonymized_return_model_as_string = self._anonymizer.deanonymize(
            return_model_as_string, anonymization_mapping
        )
        result = SummaryResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )

        return result.feedback_record_summaries[0].model_copy(
            update={"id": request.feedback_record.id, "quality_score": quality_score}
        )

    async def assign_codes(
        self,
        request: CodingAssignmentRequestModel,
        deadline: datetime,
    ) -> CodingAssignmentResultModel:
        """Assign hierarchical codes to a feedback record.

        Parameters
        ----------
        request : CodingAssignmentRequest
            Feedback records, coding framework, ``max_codes``, and tenant id.
        deadline : datetime
            Absolute UTC deadline by which all records must be coded.

        Returns
        -------
        CodingAssignmentResult
            Per-record leaf codes from ``classify_feedback``.

        Raises
        ------
        AnalysisTimeoutError
            When ``deadline`` is reached before every record is processed.
        LLMTimeoutError
            When a single LLM completion exceeds the configured timeout.
        LLMRateLimitError
            When the LLM provider returns rate limiting.
        LLMError
            For other LLM provider failures.
        """
        coded: list[CodedFeedbackRecordModel] = []
        code_level_1_nodes = request.coding_levels.root_codes
        threshold = request.confidence_threshold

        feedback_record = request.feedback_record
        self._check_coding_deadline(deadline)

        candidates: list[_ScoredCode] = []

        code_level_1_indices = await self._pick_code_indices(
            feedback_record=feedback_record,
            current_level="Code level 1",
            entries=list(code_level_1_nodes),
            hierarchy_path=None,
            tenant_id=request.tenant_id,
            deadline=deadline,
        )

        for code_level_1_index in code_level_1_indices:
            code_level_1_node = code_level_1_nodes[code_level_1_index]
            code_level_1_name = code_level_1_node.name

            judge_code_level_1 = await self._judge_code_level(
                feedback_record=feedback_record,
                level="Code level 1",
                path=[("Code level 1", code_level_1_name)],
                tenant_id=request.tenant_id,
                deadline=deadline,
            )
            if threshold is not None and judge_code_level_1.score < threshold:
                continue

            code_level_2_nodes = code_level_1_node.children
            code_level_2_indices = await self._pick_code_indices(
                feedback_record=feedback_record,
                current_level="Code level 2",
                entries=list(code_level_2_nodes),
                hierarchy_path=[("Code level 1", code_level_1_name)],
                tenant_id=request.tenant_id,
                deadline=deadline,
            )

            for code_level_2_index in code_level_2_indices:
                code_level_2_node = code_level_2_nodes[code_level_2_index]
                code_level_2_name = code_level_2_node.name

                judge_code_level_2 = await self._judge_code_level(
                    feedback_record=feedback_record,
                    level="Code level 2",
                    path=[
                        ("Code level 1", code_level_1_name),
                        ("Code level 2", code_level_2_name),
                    ],
                    tenant_id=request.tenant_id,
                    deadline=deadline,
                )
                if threshold is not None and judge_code_level_2.score < threshold:
                    continue

                code_level_3_nodes = code_level_2_node.children
                code_level_3_indices = await self._pick_code_indices(
                    feedback_record=feedback_record,
                    current_level="Code level 3",
                    entries=list(code_level_3_nodes),
                    hierarchy_path=[
                        ("Code level 1", code_level_1_name),
                        ("Code level 2", code_level_2_name),
                    ],
                    tenant_id=request.tenant_id,
                    deadline=deadline,
                )

                for code_level_3_index in code_level_3_indices:
                    code_level_3_node = code_level_3_nodes[code_level_3_index]
                    code_level_3_name = code_level_3_node.name

                    judge_code_level_3 = await self._judge_code_level(
                        feedback_record=feedback_record,
                        level="Code level 3",
                        path=[
                            ("Code level 1", code_level_1_name),
                            ("Code level 2", code_level_2_name),
                            ("Code level 3", code_level_3_name),
                        ],
                        tenant_id=request.tenant_id,
                        deadline=deadline,
                    )
                    if threshold is not None and judge_code_level_3.score < threshold:
                        continue

                    candidates.append(
                        _ScoredCode(
                            coding_level_1_id=code_level_1_node.id,
                            coding_level_1_name=code_level_1_name,
                            coding_level_2_id=code_level_2_node.id,
                            coding_level_2_name=code_level_2_name,
                            coding_level_3_id=code_level_3_node.id,
                            coding_level_3_name=code_level_3_name,
                            confidence_level_1=judge_code_level_1.score,
                            confidence_level_2=judge_code_level_2.score,
                            confidence_level_3=judge_code_level_3.score,
                            explanation_level_1=judge_code_level_1.explanation,
                            explanation_level_2=judge_code_level_2.explanation,
                            explanation_level_3=judge_code_level_3.explanation,
                        )
                    )

        candidates.sort(key=lambda c: c.confidence_aggregate, reverse=True)
        top = candidates[: request.max_codes]

        coded.append(
            CodedFeedbackRecordModel(
                feedback_record_id=feedback_record.id,
                assigned_codes=tuple(
                    AssignedCodeModel(
                        coding_level_1_id=c.coding_level_1_id,
                        coding_level_1_name=c.coding_level_1_name,
                        coding_level_2_id=c.coding_level_2_id,
                        coding_level_2_name=c.coding_level_2_name,
                        coding_level_3_id=c.coding_level_3_id,
                        coding_level_3_name=c.coding_level_3_name,
                        confidence_code_level_1=c.confidence_level_1,
                        confidence_code_level_2=c.confidence_level_2,
                        confidence_code_level_3=c.confidence_level_3,
                        confidence_aggregate=c.confidence_aggregate,
                        explanation=c.explanation,
                    )
                    for c in top
                ),
            )
        )

        return CodingAssignmentResultModel(coded_feedback_records=tuple(coded))

    async def detect_sensitive_content(
        self,
        request: SensitivityAnalysisRequestModel,
        deadline: datetime,
    ) -> SensitivityAnalysisResultModel:
        """Detect sensitive content in a single feedback record.

        Parameters
        ----------
        request : SensitivityAnalysisRequestModel
            The sensitivity analysis request containing a single feedback record.

        Returns
        -------
        SensitivityAnalysisResultModel
            The sensitivity analysis result for the feedback record.
        """
        timeout = self._check_deadline_and_get_timeout(deadline)
        system_message = _DEFAULT_SENSITIVITY_DETECTION_PROMPT
        user_message = build_feedback_record_envelope(
            request.feedback_record, include_metadata=True, include_id=True
        )

        anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
            user_message
        )

        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=SensitivityAnalysisResultModelList,
            timeout=timeout,
        )

        return_model_as_string = response.structured.model_dump_json()
        unanonymized_return_model_as_string = self._anonymizer.deanonymize(
            return_model_as_string, anonymization_mapping
        )
        structured = SensitivityAnalysisResultModelList.model_validate_json(
            unanonymized_return_model_as_string
        )

        raw = structured.results[0] if structured.results else None
        return SensitivityAnalysisResultModel(
            feedback_record_id=request.feedback_record.id,
            sensitivity_types=raw.sensitivity_types if raw else (),
            explanation=raw.explanation if raw else "No sensitive content detected.",
        )

    def _check_deadline_and_get_timeout(self, deadline: datetime) -> float:
        """Raise if the deadline has passed or too little time remains.

        Return a timeout (seconds) bounded by the deadline and the
        configured per-call limit.
        """
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise AnalysisTimeoutError("Deadline exceeded")
        if remaining < _MINIMUM_ATTEMPT_WINDOW:
            raise AnalysisTimeoutError(
                f"Insufficient time remaining ({remaining:.1f}s) for an LLM attempt"
            )
        # A single ``LLMPort.complete`` may retry internally, spending up to
        # ``LLM_RETRY_BUDGET_MULTIPLIER`` times this per-attempt timeout in worst
        # case (see ``qfa.adapters.llm_client``). Divide the remaining deadline
        # by the same factor so even a fully-retried last call finishes before
        # the deadline. With a generous deadline the per-call cap binds first, so
        # this only bites as the deadline approaches.
        per_attempt_budget = remaining / LLM_RETRY_BUDGET_MULTIPLIER
        return min(self._llm_timeout_seconds, per_attempt_budget)

    def _check_coding_deadline(self, deadline: datetime) -> None:
        """Raise when the coding deadline is exceeded."""
        if datetime.now(UTC) >= deadline:
            raise AnalysisTimeoutError(
                "Coding deadline exceeded before all feedback records were processed"
            )

    async def _pick_code_indices(
        self,
        *,
        feedback_record: FeedbackRecordModel,
        current_level: str,
        entries: list[CodingNode],
        hierarchy_path: list[tuple[str, str]] | None,
        tenant_id: str,
        deadline: datetime,
    ) -> list[int]:
        """Build one coding prompt, call the LLM, and parse selected indices."""
        labels = [entry.name for entry in entries]
        system_message, user_message = build_pick_messages(
            feedback_record=feedback_record,
            current_level=current_level,
            labels=labels,
            hierarchy_path=hierarchy_path,
        )
        if not user_message:
            return []

        self._check_coding_deadline(deadline)
        self._check_token_limit(system_message, user_message)

        anonymized_user_message, _ = self._anonymizer.anonymize(user_message)

        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=tenant_id,
            response_model=str,
        )
        return parse_selected_indices(response.structured, len(labels))

    async def _judge_code_level(
        self,
        *,
        feedback_record: FeedbackRecordModel,
        level: str,
        path: list[tuple[str, str]],
        tenant_id: str,
        deadline: datetime,
    ) -> JudgeResponse:
        """Call the judge LLM for one hierarchy level; return structured score and explanation."""
        system_message, user_message = build_judge_messages(
            feedback_record=feedback_record,
            level=level,
            path=path,
        )
        self._check_coding_deadline(deadline)
        self._check_token_limit(system_message, user_message)
        user_message, _ = self._anonymizer.anonymize(user_message)
        response = await self._llm.complete(
            system_message=system_message,
            user_message=user_message,
            tenant_id=tenant_id,
            response_model=JudgeResponse,
        )
        if not 0.0 <= response.structured.score <= 1.0:
            raise AnalysisError("LLM judge returned score outside 0.0-1.0")
        return response.structured

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    def _check_token_limit(self, system_message: str, user_message: str) -> None:
        """Estimate total tokens and raise if over the limit.

        Parameters
        ----------
        system_message : str
            The assembled system message.
        user_message : str
            The assembled user message containing the feedback records.

        Raises
        ------
        FeedbackTooLargeError
            When estimated tokens exceed the configured limit.
        """
        assembled_text = system_message + user_message
        estimated_tokens = len(assembled_text) // self._settings.chars_per_token
        if estimated_tokens > self._max_total_tokens:
            msg = (
                f"Estimated tokens ({estimated_tokens}) exceed limit "
                f"({self._max_total_tokens})"
            )
            raise FeedbackTooLargeError(
                msg,
                estimated_tokens=estimated_tokens,
                limit=self._max_total_tokens,
            )
