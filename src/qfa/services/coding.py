"""Coding service — assign hierarchical codes to a feedback record.

Backs ``POST /v1/assign-codes``. Picking is one shot: the full coding
framework is flattened into one option per node (at every depth, not just
leaves), and a single LLM call selects the best-fitting path(s) directly —
no recursive per-level picking. Judging is per level: each selected path is
then scored level by level by a separate judge call per level, stopping at
the first level that falls below ``confidence_threshold``. Candidates below
the threshold are dropped, and the ones that survive are ranked and
truncated to ``max_codes``.

This is the only use case whose pick step selects from the whole flattened
framework rather than a fixed sequence of calls, which is why it lives in
its own module (ADR-017): its private helpers are used by nothing else.

Per ADR-017 :class:`CodingService` has **no base class**. The scaffolding
it shares with the other use cases — the token-budget guard and the
deadline→timeout derivation — comes from the injected
:class:`~qfa.services.llm_call_executor.LLMCallExecutor`, and everything
else it needs (the LLM connection, the anonymiser) is named explicitly in
its constructor.

The one-shot pick runs on the **primary** LLM connection; the per-level
judge runs on the judge connection, which #310 extended #258's split to
cover. With no ``JUDGE_LLM_MODEL`` configured the two are the same client.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from qfa.domain.errors import AnalysisError, AnalysisTimeoutError, LLMResponseParseError
from qfa.domain.models import (
    AssignedCodeModel,
    CodedFeedbackRecordModel,
    CodingAssignmentRequestModel,
    CodingAssignmentResultModel,
    FeedbackRecordModel,
)
from qfa.domain.ports import AnonymizationPort, LLMPort
from qfa.services.coding_classifier import (
    CodingResponse,
    JudgeResponse,
    build_coding_messages,
    build_judge_messages,
    flatten_coding_nodes,
    format_code_path,
)
from qfa.services.llm_call_executor import LLMCallExecutor

logger = logging.getLogger(__name__)


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
        f"{format_code_path(c.path)} — "
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


_JUDGE_RESPONSE_PATTERN = re.compile(
    r"score:\s*(?P<score>-?[0-9.]+)\s*\n\s*explanation:\s*(?P<explanation>.+)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_judge_response(raw: str) -> JudgeResponse:
    """Parse the judge LLM's free-text ``SCORE:``/``EXPLANATION:`` reply.

    Mirrors ``summarize._parse_judge_quality_score``'s pattern (a provider
    that cannot be asked to enforce a response schema, see
    :class:`~qfa.services.coding_classifier.JudgeResponse`), extended to
    the two fields this judge reports. Raises ``AnalysisError``, same class
    and message-shape summarize already uses for a malformed judge reply,
    on anything that doesn't match ``_JUDGE_SYSTEM``'s output-format
    instruction.
    """
    match = _JUDGE_RESPONSE_PATTERN.search(raw)
    if match is None:
        raise AnalysisError("LLM judge returned an unparsable response")
    try:
        score = float(match.group("score"))
    except ValueError as exc:
        raise AnalysisError("LLM judge returned an unparsable response") from exc
    return JudgeResponse(score=score, explanation=match.group("explanation").strip())


class CodingService:
    """Assign hierarchical codes to a feedback record via pick/judge calls.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter used for the one-shot pick — the only
        generation call this service makes.
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII from each assembled
        prompt before it leaves the process.
    executor : LLMCallExecutor
        The shared LLM-call scaffolding (ADR-017), used here for the
        pre-flight token-budget guard and deadline-derived timeout.
        Injected rather than self-constructed so the composition root stays
        the one place the object graph is assembled.
    judge_llm : LLMPort | None
        Optional separate adapter for the per-level judge calls, so judging
        can run on a different model than the pick. ``None`` (the default)
        routes judge calls to ``llm``, which is the behaviour when no
        ``JUDGE_LLM_MODEL`` is configured. Configured via ``JUDGE_LLM_*``
        and resolved in
        :func:`qfa.api.composition.resolve_judge_llm_settings`.
    """

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        executor: LLMCallExecutor,
        judge_llm: LLMPort | None = None,
    ) -> None:
        self._llm = llm
        # Same rule as AnalyzeService/SummarizeService: judging runs on its
        # own connection when one is configured, so the generator does not
        # grade its own output, and falling back to the primary keeps the
        # default identical while call sites never branch.
        self._judge_llm = judge_llm if judge_llm is not None else llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._executor = executor

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
        except LLMResponseParseError as exc:
            # Malformed/unparseable pick output is treated as a genuine
            # empty pick rather than a request failure, matching the old
            # per-level pick step's tolerance for bad LLM output.
            logger.warning(
                "Coding pick call failed to parse: error_class=%s",
                type(exc).__name__,
            )
            selected_indices = []

        valid_indices = [
            idx for idx in dict.fromkeys(selected_indices) if 0 <= idx < len(options)
        ]
        judged = await asyncio.gather(
            *(
                self._judge_selected_path(
                    feedback_record=feedback_record,
                    path=options[idx].path,
                    threshold=request.confidence_threshold,
                    tenant_id=request.tenant_id,
                    deadline=deadline,
                )
                for idx in valid_indices
            )
        )

        candidates: list[_ScoredCode] = []
        rejected: list[_ScoredCode] = []
        for scored, was_rejected in judged:
            (rejected if was_rejected else candidates).append(scored)

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
    ) -> tuple[_ScoredCode, bool]:
        """Judge a one-shot-selected path level by level, root to leaf.

        Reproduces the previous per-level pick/judge design's judge step
        exactly — same prompt, same score/explanation contract, same
        early-stop-on-rejection behaviour — the only difference being that
        the path being judged was already chosen in one shot rather than
        picked one level at a time.

        Returns the scored path and whether it was rejected by
        ``threshold``, so independently-selected paths can be judged
        concurrently instead of one at a time.
        """
        scores: list[float] = []
        explanations: list[str] = []
        hierarchy_path: list[tuple[str, str]] = []
        for level_num, (_, name) in enumerate(path, start=1):
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
                return (
                    _ScoredCode(
                        path=list(path[:level_num]),
                        scores=scores,
                        explanations=explanations,
                    ),
                    True,
                )
            hierarchy_path = current_path
        return (
            _ScoredCode(path=list(path), scores=scores, explanations=explanations),
            False,
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
        """Call the judge LLM for one hierarchy level; return score and explanation.

        Free-text, not schema-enforced structured output — see
        :class:`~qfa.services.coding_classifier.JudgeResponse`'s docstring
        for why the judge connection cannot rely on the provider to enforce
        a response schema here.
        """
        system_message, user_message = build_judge_messages(
            feedback_record=feedback_record,
            level=level,
            path=path,
        )
        self._check_coding_deadline(deadline)
        self._executor.check_token_limit(system_message, user_message)
        user_message, _ = self._anonymizer.anonymize(user_message)
        response = await self._judge_llm.complete(
            system_message=system_message,
            user_message=user_message,
            tenant_id=tenant_id,
            response_model=str,
        )
        judged = _parse_judge_response(response.structured)
        if not 0.0 <= judged.score <= 1.0:
            raise AnalysisError("LLM judge returned score outside 0.0-1.0")
        return judged

    def _check_coding_deadline(self, deadline: datetime) -> None:
        """Raise when the coding deadline is exceeded."""
        if datetime.now(UTC) >= deadline:
            raise AnalysisTimeoutError(
                "Coding deadline exceeded before all feedback records were processed"
            )
