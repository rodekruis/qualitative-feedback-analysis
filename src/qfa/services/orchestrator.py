"""Orchestrator service — core business logic for feedback analysis.

Assembles prompts, enforces token limits, filters prompt injection,
manages retries with exponential backoff, and enforces deadlines.

The scaffolding those last three share across use cases — anonymise a batch
of records, derive a per-call timeout from the deadline, guard the token
budget, run a semaphore-bounded completion — lives on the injected
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` rather than on this
class (ADR-017).
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qfa.domain.chunk_models import Chunk
from qfa.domain.clustering_models import CodingTrendTable
from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    LLMError,
    LLMRateLimitError,
    LLMResponseParseError,
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
    FeedbackRecordModel,
    FeedbackRecordSummaryModel,
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityAnalysisResultModelList,
    SingleSummaryRequestModel,
    SummaryRequestModel,
    SummaryResultModel,
)
from qfa.domain.ports import AnonymizationPort, EmbeddingPort, LLMPort
from qfa.domain.sensitivity_types import SENSITIVITY_TYPE_DESCRIPTIONS
from qfa.services.clustering import cluster_records
from qfa.services.coding_classifier import (
    CodingResponse,
    JudgeResponse,
    build_coding_messages,
    build_judge_messages,
    flatten_coding_nodes,
)
from qfa.services.coding_trends import build_coding_trend_table
from qfa.services.hierarchical_prompts import (
    build_map_system_message,
    build_reduce_system_message,
    build_reduce_user_message,
)
from qfa.services.llm_call_executor import LLMCallExecutor, SlotTiming
from qfa.services.prompts import (
    ANALYZE_ACTION_PROMPT,
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
    "Do not end with a question, an offer of further help, or an invitation for follow-up input.\n"
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
    "Do not end with a question, an offer of further help, or an invitation for follow-up input.\n"
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


def _json_escape_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Escape mapping values for safe substitution into a JSON string.

    ``AnonymizationPort.deanonymize`` does a raw substring replace, so
    restoring a PII value that contains a quote, backslash, or control
    character (e.g. a newline in an address) directly into an
    already-serialized JSON string corrupts it. Escaping each value the
    way ``json.dumps`` would keeps the result valid JSON.
    """
    return {
        placeholder: json.dumps(value)[1:-1] for placeholder, value in mapping.items()
    }


def _hyperlink_form_references(
    text: str,
    feedback_records: tuple[FeedbackRecordModel, ...],
    espo_feedback_base_url: str | None,
) -> str:
    """Rewrite feedback-record-id mentions in ``text`` as EspoCRM hyperlinks.

    When the analysis/summary text names a feedback record by its ``id``
    (e.g. ``Form-07762``), rewrite that mention as
    ``[Form-07762](espo_feedback_base_url/url_id)`` so it renders as a
    clickable link back to the record in EspoCRM. No-op when
    ``espo_feedback_base_url`` is not provided; per-record no-op when that
    record has no ``url_id``. Matches on word boundaries so one record's id
    cannot match as a substring of another's (e.g. ``Form-1`` vs
    ``Form-10``).
    """
    if not espo_feedback_base_url:
        return text
    base = espo_feedback_base_url.rstrip("/")
    for record in feedback_records:
        if not record.url_id:
            continue
        link = f"[{record.id}]({base}/{record.url_id})"
        text = re.sub(rf"\b{re.escape(record.id)}\b", link, text)
    return text


@dataclass
class _ScoredCode:
    path: list[tuple[str, str]]  # (id, name) per level, root → leaf
    scores: list[float]  # per-level judge scores, aligned with path
    explanations: list[str]  # per-level judge explanations, aligned with path

    @property
    def confidence_aggregate(self) -> float:
        return min(self.scores)

    @property
    def explanation(self) -> str:
        return "\n".join(
            f"- Level {i + 1} ({score:.2f}): {expl}"
            for i, (score, expl) in enumerate(zip(self.scores, self.explanations))
        )

    @property
    def decisive_explanation(self) -> str:
        """The judge explanation for the level that decided this candidate.

        Judging a selected path stops at the first level that falls below
        the threshold, so for a *rejected* candidate the last accumulated
        level is both its lowest-scoring one and the reason it was dropped.
        The levels before it passed and would only add noise to a message
        whose whole point is "why was nothing applied".
        """
        return self.explanations[-1]


NO_CODING_LEAD = "NO CODING APPLIED."
"""Literal first line of every explanation returned when no code is applied.

EspoCRM surfaces ``assigned_codes.0.explanation`` verbatim as
``autoCodingExplanation``, so this line is what a user reads first when a
record comes back uncoded (#256).
"""

NO_CODING_EMPTY_CONTENT_EXPLANATION = (
    f"{NO_CODING_LEAD}\nThe feedback text was empty, so there was nothing to code."
)
"""Explanation for a record whose ``content`` is empty (issue #138)."""

NO_CODING_NOTHING_RELEVANT_EXPLANATION = (
    f"{NO_CODING_LEAD}\nNo code in the framework was judged relevant to this feedback."
)
"""Explanation for when the LLM selected nothing at all, or nothing it selected survived judging."""

_MAX_LISTED_REJECTIONS = 3
"""How many near-miss candidates to spell out before collapsing to a count."""


def _as_whole_percentage(confidence: float) -> str:
    """Render a 0-1 confidence as a whole percentage (``0.04`` -> ``"4%"``).

    Non-technical EspoCRM readers see these numbers directly, and a
    ``0.04`` next to a code label reads as noise where "4%" reads as a
    judgement.
    """
    return f"{round(confidence * 100)}%"


def _combine_rejected_explanations(
    rejected: list[_ScoredCode], threshold: float
) -> str:
    """Explain in prose why every candidate was rejected by the threshold.

    Leads with :data:`NO_CODING_LEAD` and a sentence naming the threshold,
    then lists at most :data:`_MAX_LISTED_REJECTIONS` candidates —
    highest-scoring (closest to being applied) first — as a
    ``path — percentage`` header over the decisive level's explanation.
    Any remainder collapses into a single count line rather than an
    unbounded wall of text.
    """
    ordered = sorted(rejected, key=lambda c: c.confidence_aggregate, reverse=True)
    listed = ordered[:_MAX_LISTED_REJECTIONS]

    blocks = [
        f"{NO_CODING_LEAD}\n"
        f"No code reached the {_as_whole_percentage(threshold)} confidence "
        f"threshold, so this record needs human review."
    ]
    blocks += [
        f"{' > '.join(name for _, name in c.path)} — "
        f"{_as_whole_percentage(c.confidence_aggregate)}\n"
        f"  {c.decisive_explanation}"
        for c in listed
    ]

    remainder = len(ordered) - len(listed)
    if remainder:
        noun = "code" if remainder == 1 else "codes"
        cutoff = _as_whole_percentage(listed[-1].confidence_aggregate)
        blocks.append(f"{remainder} further {noun} scored below {cutoff}.")

    return "\n\n".join(blocks)


class Orchestrator:
    """Core orchestration service for feedback analysis.

    Assembles prompts from feedback records, validates input,
    calls the LLM through the ``LLMPort``, and manages retries
    with exponential backoff and deadline enforcement.

    Deadline arithmetic, the token-budget guard, batch anonymisation and
    semaphore-bounded completions are delegated to the injected
    :class:`~qfa.services.llm_call_executor.LLMCallExecutor` (``executor``
    below), so this class holds use-case logic rather than call scaffolding.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter used for every generation call (analysis,
        hierarchical map/reduce, summarisation, code assignment).
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
    judge_llm : LLMPort | None
        Optional separate adapter for the LLM-as-judge quality-score calls,
        so judging can run on a different model than generation. ``None``
        (the default) routes judge calls to ``llm``, which is the behaviour
        when no ``JUDGE_LLM_MODEL`` is configured. Configured via
        ``JUDGE_LLM_*`` and resolved in
        :func:`qfa.api.composition.resolve_judge_llm_settings`.

        Four call sites use it: the ``analyze`` judge, the hierarchical leaf
        judges, and the judges in ``summarize_aggregate`` and ``summarize``.
        The per-level judge inside ``assign_codes`` deliberately stays on
        ``llm``.
    executor : LLMCallExecutor | None
        The shared LLM-call scaffolding (anonymise-records, deadline→timeout
        derivation, token-budget guard, semaphore-bounded completion) this
        orchestrator delegates to, per ADR-017. The composition root
        (:func:`qfa.api.composition.build_orchestrator`) constructs it
        explicitly. ``None`` (the default) builds one over the same ``llm``,
        ``anonymizer``, ``settings``, ``llm_timeout_seconds`` and
        ``max_total_tokens`` this constructor already received, so callers
        that don't care about the collaborator — scripts, notebooks, and the
        bulk of the test suite — need not thread it through.
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
        judge_llm: LLMPort | None = None,
        executor: LLMCallExecutor | None = None,
    ) -> None:
        self._llm = llm
        # Judge calls run on their own connection when one is configured, so
        # the generator does not grade its own output. Falling back to the
        # primary client keeps the default (no JUDGE_LLM_MODEL) behaviour
        # identical to before the judge connection existed, and means call
        # sites never branch — they just use _judge_llm.
        self._judge_llm = judge_llm if judge_llm is not None else llm
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
        # Shared LLM-call scaffolding (ADR-017): an injected collaborator, not a
        # base class. Default-constructed from the arguments above so the
        # composition root can inject one without every other construction site
        # having to. Either way it is built over the *primary* llm; judge calls
        # pass ``llm=self._judge_llm`` per call.
        self._executor = executor or LLMCallExecutor(
            llm=llm,
            anonymizer=anonymizer,
            settings=settings,
            llm_timeout_seconds=llm_timeout_seconds,
            max_total_tokens=max_total_tokens,
        )

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

        analyse_timeout = self._executor.check_deadline_and_get_timeout(deadline)
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

        analysis_text = _hyperlink_form_references(
            analysis_text, request.feedback_records, request.espo_feedback_base_url
        )

        quality_score: float | None
        uncertainty_explanation: str
        try:
            judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
            judge_system = build_analyze_judge_system_message(
                source_text=anonymized_user_message,
                analyst_prompt=anonymized_prompt,
                analysis=analyse_response.structured,
                output_language=request.output_language,
            )
            judge_response = await self._judge_llm.complete(
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
            code_fields=self._analyze_settings.coding_trend_code_fields,
            period=(
                request.period or self._analyze_settings.default_coding_trend_period
            ),
        )

        return AnalysisResultModel(
            result=analysis_text,
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
            anonymized_records, mapping = self._executor.anonymize_records(
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
            timing = SlotTiming()
            with timed() as chunk_sw:
                partial = await self._map_chunk(
                    anonymized_prompt,
                    chunk.records,
                    request.tenant_id,
                    deadline,
                    semaphore,
                    timing=timing,
                    output_language=request.output_language,
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
                timing = SlotTiming()
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

        analysis_text = _hyperlink_form_references(
            analysis_text, request.feedback_records, request.espo_feedback_base_url
        )

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
            result=analysis_text,
            confidence=confidence,
            uncertainty_explanation=uncertainty,
            coding_trends=trend_table,
        )

    async def _map_chunk(
        self,
        analyst_prompt: str,
        records: tuple[FeedbackRecordModel, ...],
        tenant_id: str,
        deadline: datetime,
        semaphore: asyncio.Semaphore,
        timing: SlotTiming | None = None,
        output_language: str | None = None,
    ) -> str:
        """Produce one partial analysis for a chunk (no judging).

        The leaf judge that scores this partial runs as a separate phase
        (see :meth:`_judge_chunk`), after reduce — both depend only on the
        partials, and deferring the judges keeps a time-starved judge phase
        from discarding the already-produced synthesis.
        """
        response = await self._executor.bounded_complete(
            semaphore,
            llm=self._llm,
            system_message=build_map_system_message(output_language),
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
        timing: SlotTiming | None = None,
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
            # No output_language here: only quality_score below is used, and
            # this leaf judge's uncertainty_explanation is discarded (the
            # hierarchical result's uncertainty_explanation is a deterministic
            # string built from the aggregated scores, not LLM text).
            judge_system = build_analyze_judge_system_message(
                source_text=user_message,
                analyst_prompt=analyst_prompt,
                analysis=partial,
            )
            judge_response = await self._executor.bounded_complete(
                semaphore,
                llm=self._judge_llm,
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
            response = await self._executor.bounded_complete(
                semaphore,
                llm=self._llm,
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

        timeout = self._executor.check_deadline_and_get_timeout(deadline)
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

        judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
        judge_response = await self._judge_llm.complete(
            system_message=judge_system,
            user_message=_JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        total_cost += judge_response.cost
        quality_score = _parse_judge_quality_score(judge_response.structured)

        response.structured.quality_score = quality_score

        return_model_as_string = response.structured.model_dump_json()
        unanonymized_return_model_as_string = self._anonymizer.deanonymize(
            return_model_as_string, _json_escape_mapping(anonymization_mapping)
        )
        result = AggregateSummaryResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )
        return result.model_copy(
            update={
                "summary": _hyperlink_form_references(
                    result.summary,
                    request.feedback_records,
                    request.espo_feedback_base_url,
                )
            }
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
        timeout = self._executor.check_deadline_and_get_timeout(deadline)
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
        judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
        judge_response = await self._judge_llm.complete(
            system_message=judge_system,
            user_message=_JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        quality_score = _parse_judge_quality_score(judge_response.structured)

        return_model_as_string = llm_completion.structured.model_dump_json()
        unanonymized_return_model_as_string = self._anonymizer.deanonymize(
            return_model_as_string, _json_escape_mapping(anonymization_mapping)
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

        Picking is one shot: the full coding framework is flattened into one
        option per node (at every depth, not just leaves), and a single LLM
        call selects the best-fitting path(s) directly — no recursive
        per-level picking. Judging is unchanged from the per-level design: each
        selected path is then scored level by level by a separate judge call
        per level, stopping at the first level that falls below
        ``confidence_threshold``, exactly as when picking was also per-level.

        Parameters
        ----------
        request : CodingAssignmentRequest
            Feedback records, coding framework, ``max_codes``, and tenant id.
        deadline : datetime
            Absolute UTC deadline by which all records must be coded.

        Returns
        -------
        CodingAssignmentResult
            Per-record codes from the judge, ordered by confidence, highest
            first. ``assigned_codes`` is never empty: when no code is applied
            it holds exactly one entry with null ``coding_level_*``/
            ``confidence_*`` fields and an ``explanation`` leading with
            ``NO CODING APPLIED.`` (#256). That explanation lists the near
            misses when ``confidence_threshold`` filtered every candidate
            out, and states that nothing was relevant when nothing was
            selected at all.

        Raises
        ------
        AnalysisTimeoutError
            When ``deadline`` is reached before every record is processed.
        AnalysisError
            When the judge returns a score outside 0.0-1.0.
        LLMTimeoutError
            When a single LLM completion exceeds the configured timeout.
        LLMRateLimitError
            When the LLM provider returns rate limiting.
        LLMError
            For other LLM provider failures. A pick response that fails
            schema validation (``LLMResponseParseError``) is treated as an
            empty pick instead of being raised.
        """
        feedback_record = request.feedback_record
        self._check_coding_deadline(deadline)

        options = flatten_coding_nodes(list(request.coding_levels.root_codes))
        system_message, user_message = build_coding_messages(
            feedback_record=feedback_record, options=options
        )

        if not user_message:
            # No options to select from (empty coding framework): this is
            # equivalent to a genuine empty pick, so explain it the same way
            # rather than returning a bare empty list (#256).
            coded = [
                CodedFeedbackRecordModel(
                    feedback_record_id=feedback_record.id,
                    assigned_codes=(
                        AssignedCodeModel(
                            explanation=NO_CODING_NOTHING_RELEVANT_EXPLANATION
                        ),
                    ),
                )
            ]
            return CodingAssignmentResultModel(coded_feedback_records=tuple(coded))

        self._executor.check_token_limit(system_message, user_message)
        anonymized_user_message, _ = self._anonymizer.anonymize(user_message)
        timeout = self._executor.check_deadline_and_get_timeout(deadline)

        try:
            response = await self._llm.complete(
                system_message=system_message,
                user_message=anonymized_user_message,
                tenant_id=request.tenant_id,
                response_model=CodingResponse,
                timeout=timeout,
            )
            selected_indices = response.structured.selected
        except LLMResponseParseError:
            # Malformed/unparseable pick output is treated as a genuine
            # empty pick rather than a request failure, matching the old
            # per-level pick step's tolerance for bad LLM output.
            selected_indices = []

        candidates: list[_ScoredCode] = []
        rejected: list[_ScoredCode] = []
        seen_indices: set[int] = set()
        for idx in selected_indices:
            if not 0 <= idx < len(options):
                continue
            if idx in seen_indices:
                continue
            seen_indices.add(idx)
            await self._judge_selected_path(
                feedback_record=feedback_record,
                path=options[idx].path,
                threshold=request.confidence_threshold,
                tenant_id=request.tenant_id,
                deadline=deadline,
                candidates=candidates,
                rejected=rejected,
            )

        candidates.sort(key=lambda c: c.confidence_aggregate, reverse=True)
        top = candidates[: request.max_codes]

        assigned_codes: list[AssignedCodeModel]
        if top:
            assigned_codes = [
                AssignedCodeModel(
                    coding_level_1_id=c.path[0][0],
                    coding_level_1_name=c.path[0][1],
                    coding_level_2_id=c.path[1][0] if len(c.path) > 1 else None,
                    coding_level_2_name=c.path[1][1] if len(c.path) > 1 else None,
                    coding_level_3_id=c.path[2][0] if len(c.path) > 2 else None,
                    coding_level_3_name=c.path[2][1] if len(c.path) > 2 else None,
                    confidence_level_1=c.scores[0],
                    confidence_level_2=c.scores[1] if len(c.scores) > 1 else None,
                    confidence_level_3=c.scores[2] if len(c.scores) > 2 else None,
                    confidence_aggregate=c.confidence_aggregate,
                    explanation=c.explanation,
                )
                for c in top
            ]
        elif rejected and request.confidence_threshold is not None:
            # Every candidate was filtered out by confidence_threshold: list
            # the near misses instead of an unexplained empty list. Nothing
            # can be rejected without a threshold, so the second condition
            # only narrows the type — it never rules a real case out.
            assigned_codes = [
                AssignedCodeModel(
                    explanation=_combine_rejected_explanations(
                        rejected, request.confidence_threshold
                    )
                )
            ]
        else:
            # Nothing was picked, or nothing picked survived judging. Still
            # return an entry so the caller never has to explain an empty
            # list to a user (#256).
            assigned_codes = [
                AssignedCodeModel(explanation=NO_CODING_NOTHING_RELEVANT_EXPLANATION)
            ]

        coded = [
            CodedFeedbackRecordModel(
                feedback_record_id=feedback_record.id,
                assigned_codes=tuple(assigned_codes),
            )
        ]

        return CodingAssignmentResultModel(coded_feedback_records=tuple(coded))

    async def _judge_selected_path(
        self,
        *,
        feedback_record: FeedbackRecordModel,
        path: tuple[tuple[str, str], ...],
        threshold: float | None,
        tenant_id: str,
        deadline: datetime,
        candidates: list[_ScoredCode],
        rejected: list[_ScoredCode],
    ) -> None:
        """Judge a one-shot-selected path level by level, root to leaf.

        Reproduces the previous per-level pick/judge design's judge step
        exactly — same prompt, same score/explanation contract, same
        early-stop-on-rejection behaviour — the only difference being that
        the path being judged was already chosen in one shot rather than
        picked one level at a time.
        """
        scores: list[float] = []
        explanations: list[str] = []
        hierarchy_path: list[tuple[str, str]] = []
        for level_num, (code_id, name) in enumerate(path, start=1):
            level_label = f"Code level {level_num}"
            current_path = [*hierarchy_path, (level_label, name)]
            judge = await self._judge_code_level(
                feedback_record=feedback_record,
                level=level_label,
                path=current_path,
                tenant_id=tenant_id,
                deadline=deadline,
            )
            scores.append(judge.score)
            explanations.append(judge.explanation)
            if threshold is not None and judge.score < threshold:
                rejected.append(
                    _ScoredCode(
                        path=list(path[:level_num]),
                        scores=scores,
                        explanations=explanations,
                    )
                )
                return
            hierarchy_path = current_path
        candidates.append(
            _ScoredCode(path=list(path), scores=scores, explanations=explanations)
        )

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
        self._executor.check_token_limit(system_message, user_message)
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
        timeout = self._executor.check_deadline_and_get_timeout(deadline)
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
            return_model_as_string, _json_escape_mapping(anonymization_mapping)
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

    def _check_coding_deadline(self, deadline: datetime) -> None:
        """Raise when the coding deadline is exceeded."""
        if datetime.now(UTC) >= deadline:
            raise AnalysisTimeoutError(
                "Coding deadline exceeded before all feedback records were processed"
            )
