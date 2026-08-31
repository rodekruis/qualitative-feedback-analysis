"""Application service for the ``POST /v1/analyze-bulk`` use case.

Holds both analyse modes — ``single_pass`` (:meth:`AnalyzeService.analyze_bulk`)
and ``hierarchical`` (:meth:`AnalyzeService.analyze_hierarchical`) — because
they are two modes of *one* endpoint, selected on the request, and they share
the retained-placeholder guardrail
(:data:`AnalyzeService._ANALYZE_RETAINED_PLACEHOLDER_TYPES`). Splitting them
would either duplicate that rule or need a third object to hold it.

Per ADR-017 this class has **no base class**: the shared LLM-call scaffolding
(anonymise a batch of records, derive a per-call timeout from the deadline,
guard the token budget, run a semaphore-bounded completion) arrives as the
injected :class:`~qfa.services.llm_call_executor.LLMCallExecutor` collaborator.
The ``embedder`` the hierarchical path needs sits on *this* constructor rather
than on one shared by every use case — it is the dependency that motivated the
composition-only decomposition in the first place.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qfa.domain.chunk_models import Chunk
from qfa.domain.clustering_models import CodingTrendTable
from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    FeedbackRecordModel,
)
from qfa.domain.ports import AnonymizationPort, EmbeddingPort, LLMPort
from qfa.services.clustering import cluster_records
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
    JUDGE_USER_MESSAGE,
    build_analyze_judge_system_message,
    build_analyze_user_message,
    build_output_language_instruction,
)
from qfa.services.record_links import hyperlink_form_references
from qfa.settings import AnalyzeSettings, OrchestratorSettings
from qfa.utils import timed

logger = logging.getLogger(__name__)


class AnalyzeJudgeResult(BaseModel):
    """Quality score + explanation for one analyse-judge call.

    Populated by parsing the judge LLM's free-text reply (see
    ``ANALYZE_JUDGE_PROMPT``'s output-format instruction and
    ``_parse_analyze_judge_response``) rather than schema-enforced
    structured output: the judge connection can point at a model/deployment
    that rejects a ``json_schema`` response format outright regardless of
    its contents (confirmed against ``azure_ai/mistral-medium-3-5`` — its
    serving backend has grammar-constrained decoding disabled), so neither
    call site that uses this model can rely on the provider to enforce the
    shape. Field-level validation (score range, non-empty explanation)
    still runs here and surfaces as a ``pydantic.ValidationError``, which
    both call sites already catch.
    """

    model_config = ConfigDict(frozen=True)

    quality_score: float = Field(ge=0.0, le=1.0)
    uncertainty_explanation: str = Field(min_length=1)


_ANALYZE_JUDGE_RESPONSE_PATTERN = re.compile(
    r"quality_score:\s*(?P<score>-?[0-9.]+)\s*\n\s*uncertainty_explanation:\s*(?P<explanation>.+)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_analyze_judge_response(raw: str) -> AnalyzeJudgeResult:
    """Parse the analyse judge's free-text ``QUALITY_SCORE:``/``UNCERTAINTY_EXPLANATION:`` reply.

    Mirrors ``coding._parse_judge_response``'s pattern (see
    :class:`AnalyzeJudgeResult`'s docstring for why this call site cannot
    rely on the provider to enforce a response schema). Only unparseable
    input raises here (``AnalysisError``, caught by both call sites);
    field-level validation (score range, non-empty explanation) is left to
    ``AnalyzeJudgeResult`` itself, which raises ``pydantic.ValidationError``
    — also already caught by both call sites.
    """
    match = _ANALYZE_JUDGE_RESPONSE_PATTERN.search(raw)
    if match is None:
        raise AnalysisError("LLM judge returned an unparsable response")
    try:
        score = float(match.group("score"))
    except ValueError as exc:
        raise AnalysisError("LLM judge returned an unparsable response") from exc
    return AnalyzeJudgeResult(
        quality_score=score,
        uncertainty_explanation=match.group("explanation").strip(),
    )


class AnalyzeService:
    """Free-text analysis of a batch of feedback records.

    Assembles prompts from feedback records, calls the LLM through the
    ``LLMPort``, and grades the result with an LLM-as-judge call. Deadline
    arithmetic, the token-budget guard, batch anonymisation and
    semaphore-bounded completions are delegated to the injected
    :class:`~qfa.services.llm_call_executor.LLMCallExecutor` (``executor``
    below), so this class holds use-case logic rather than call scaffolding.

    Parameters
    ----------
    executor : LLMCallExecutor
        The shared LLM-call scaffolding (anonymise-records, deadline→timeout
        derivation, token-budget guard, semaphore-bounded completion) this
        service delegates to, per ADR-017. It is an injected collaborator,
        not a base class: the composition root
        (:func:`qfa.api.composition.build_services`) constructs one and
        shares it with every use-case service.
    llm : LLMPort
        The LLM provider adapter used for every generation call (the
        single-pass analysis, the hierarchical map and reduce calls).
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before LLM calls.
    settings : OrchestratorSettings
        Cross-cutting configuration; this service reads ``chars_per_token``
        for the reduce-phase token estimate.
    max_total_tokens : int
        Maximum estimated total tokens for a single request, used to size
        map chunks and reduce groups.
    analyze_settings : AnalyzeSettings | None
        Configuration for the ``POST /v1/analyze-bulk`` endpoint (clustering
        knobs, coding-trend table inputs, default period). Defaults to
        :class:`AnalyzeSettings` with environment-loaded values so tests
        and callers that don't care about analyze tuning can omit it.
    embedder : EmbeddingPort | None
        Optional embedder for ``mode=hierarchical``. ``None`` makes the
        hierarchical path raise :class:`AnalysisError` at request time,
        leaving ``single_pass`` fully usable.
    judge_llm : LLMPort | None
        Optional separate adapter for the LLM-as-judge quality-score calls,
        so judging can run on a different model than generation. ``None``
        (the default) routes judge calls to ``llm``, which is the behaviour
        when no ``JUDGE_LLM_MODEL`` is configured. Configured via
        ``JUDGE_LLM_*`` and resolved in
        :func:`qfa.api.composition.resolve_judge_llm_settings`.
    """

    # Entity types whose placeholders are NOT restored in `analyze` output.
    # Defense in depth for the "do not identify individuals" guardrail in
    # `ANALYZE_GUARDRAILS_PROMPT`: even if the analyse LLM echoes a
    # placeholder we supplied, the analyst never sees the underlying name.
    # Scoped to `analyze` only — `summarize`/`assign_codes` still restore
    # all placeholders because their per-record output is meant to be
    # faithful to the source. Defined once and shared by both analyse
    # modes, which is why they live on one service.
    _ANALYZE_RETAINED_PLACEHOLDER_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"PERSON"}
    )

    def __init__(
        self,
        executor: LLMCallExecutor,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        settings: OrchestratorSettings,
        max_total_tokens: int,
        analyze_settings: AnalyzeSettings | None = None,
        embedder: EmbeddingPort | None = None,
        judge_llm: LLMPort | None = None,
    ) -> None:
        self._executor = executor
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
        # apply without forcing every construction site to thread the extra
        # argument.
        self._analyze_settings = analyze_settings or AnalyzeSettings()
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

        analysis_text = hyperlink_form_references(
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
                user_message=JUDGE_USER_MESSAGE,
                tenant_id=request.tenant_id,
                response_model=str,
                timeout=judge_timeout,
            )
            judged = _parse_analyze_judge_response(judge_response.structured)
            quality_score = judged.quality_score
            uncertainty_explanation = judged.uncertainty_explanation
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

        analysis_text = hyperlink_form_references(
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
                user_message=JUDGE_USER_MESSAGE,
                tenant_id=tenant_id,
                response_model=str,
                deadline=deadline,
                timing=timing,
            )
            return _parse_analyze_judge_response(
                judge_response.structured
            ).quality_score
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
