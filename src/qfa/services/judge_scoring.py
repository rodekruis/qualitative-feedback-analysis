"""Shared judge-scoring utilities for the analyse, summarise and coding use cases.

Reused by :mod:`qfa.services.analyze` (single_pass and the hierarchical
leaf judge), :mod:`qfa.services.summarize` (#351, #352, #354), and
:mod:`qfa.services.coding`, so the component regex and the live-scoring
calls each live in one place rather than becoming a fourth copy per use
case.
"""

from __future__ import annotations

import logging
import re

from pydantic import ValidationError

from qfa.domain.errors import AnalysisError
from qfa.domain.models import JudgeComponents
from qfa.domain.ports import EvaluationPort
from qfa.services.call_context import current_call_context

_COMPONENTS_PATTERN = re.compile(
    r"faithfulness:\s*(?P<faithfulness>-?[0-9.]+)\s*\n"
    r"\s*coverage:\s*(?P<coverage>-?[0-9.]+)\s*\n"
    r"\s*clarity:\s*(?P<clarity>-?[0-9.]+)",
    re.IGNORECASE,
)

_MISSING_CALL_ID = "-"


def parse_judge_components(raw: str) -> JudgeComponents:
    """Extract FAITHFULNESS / COVERAGE / CLARITY from a judge reply.

    Raises :class:`~qfa.domain.errors.AnalysisError` for both unparseable
    input and out-of-range values so callers never see a
    ``pydantic.ValidationError``: :mod:`qfa.services.summarize` maps only
    ``AnalysisError`` → 502, and a leaked ``ValidationError`` there would
    be a 500.
    """
    match = _COMPONENTS_PATTERN.search(raw)
    if match is None:
        raise AnalysisError("LLM judge returned an unparsable response")
    try:
        faithfulness = float(match.group("faithfulness"))
        coverage = float(match.group("coverage"))
        clarity = float(match.group("clarity"))
    except ValueError as exc:
        raise AnalysisError("LLM judge returned an unparsable response") from exc
    try:
        return JudgeComponents(
            faithfulness=faithfulness,
            coverage=coverage,
            clarity=clarity,
        )
    except ValidationError as exc:
        raise AnalysisError("LLM judge component out of range [0, 1]") from exc


def log_judge_components(logger: logging.Logger, components: JudgeComponents) -> None:
    """Emit one ``judge components:`` line on *logger*.

    *logger* is the caller's logger so the line is attributed to the use
    case (analyse vs summarise), not this module. ``call_id`` comes from
    :data:`~qfa.services.call_context.current_call_context`; outside an
    HTTP request that is ``None``, and the line uses ``-`` rather than
    raising. Scores only — never the uncertainty explanation.
    """
    ctx = current_call_context.get()
    call_id = _MISSING_CALL_ID if ctx is None else str(ctx.call_id)
    logger.info(
        "judge components: call_id=%s faithfulness=%.3f coverage=%.3f "
        "clarity=%.3f quality_score=%.3f",
        call_id,
        components.faithfulness,
        components.coverage,
        components.clarity,
        components.quality_score,
    )


def record_judge_scores(
    evaluator: EvaluationPort | None, components: JudgeComponents
) -> None:
    """Send *components* to *evaluator* as four named scores (#354).

    Runs beside :func:`log_judge_components`, not instead of it — the log
    line stays for grep-based debugging, Langfuse becomes the durable,
    queryable copy. A no-op when *evaluator* is ``None`` (a service
    constructed without one, e.g. by a test that does not care about
    evaluation — production wiring always injects a real
    :class:`~qfa.domain.ports.EvaluationPort`, never ``None``, per
    :func:`qfa.api.composition.build_evaluator`) or when
    :data:`~qfa.services.call_context.current_call_context` is ``None``
    (outside an HTTP request, so there is no ``call_id`` to key a trace
    on).
    """
    if evaluator is None:
        return
    ctx = current_call_context.get()
    if ctx is None:
        return
    trace_id = ctx.call_id.hex
    evaluator.record_score(
        trace_id=trace_id, name="faithfulness", value=components.faithfulness
    )
    evaluator.record_score(
        trace_id=trace_id, name="coverage", value=components.coverage
    )
    evaluator.record_score(trace_id=trace_id, name="clarity", value=components.clarity)
    evaluator.record_score(
        trace_id=trace_id, name="quality_score", value=components.quality_score
    )


def record_coding_judge_score(
    evaluator: EvaluationPort | None, *, level_num: int, score: float
) -> None:
    """Send one hierarchy level's judge *score* to *evaluator*.

    Coding's judge reports one score per level, not a fixed
    :class:`JudgeComponents` set, so this sends one call rather than
    :func:`record_judge_scores`'s four. Named ``confidence_level_<n>``,
    matching :class:`~qfa.domain.models.AssignedCodeModel`'s own
    ``confidence_level_1``/``confidence_level_2``/``confidence_level_3``
    fields. ``CodingService`` stops judging a path at the first
    below-threshold level, so a given trace can carry fewer
    ``confidence_level_*`` scores than the path is deep, or several sets
    of them when more than one candidate path is judged. Same no-op rules
    as :func:`record_judge_scores`: skipped when *evaluator* is ``None``,
    or when :data:`~qfa.services.call_context.current_call_context` is
    ``None`` (outside an HTTP request, so there is no ``call_id`` to key a
    trace on).
    """
    if evaluator is None:
        return
    ctx = current_call_context.get()
    if ctx is None:
        return
    evaluator.record_score(
        trace_id=ctx.call_id.hex,
        name=f"confidence_level_{level_num}",
        value=score,
    )
