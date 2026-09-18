"""Shared judge-scoring utilities for the analyse and summarise use cases.

Reused by :mod:`qfa.services.analyze` (single_pass, #351) and
:mod:`qfa.services.summarize` (#352) so the component regex lives in one
place rather than becoming a fourth copy.
"""

from __future__ import annotations

import logging
import re

from pydantic import ValidationError

from qfa.domain.errors import AnalysisError
from qfa.domain.models import JudgeComponents
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
