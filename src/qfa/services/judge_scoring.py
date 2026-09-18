"""Shared judge-scoring utilities for the analyse and summarise use cases.

Reused by :mod:`qfa.services.analyze` (single_pass, #351) and
:mod:`qfa.services.summarize` (#352) so the component regex lives in one
place rather than becoming a fourth copy.
"""

import re

from pydantic import ValidationError

from qfa.domain.errors import AnalysisError
from qfa.domain.models import JudgeComponents

_COMPONENTS_PATTERN = re.compile(
    r"faithfulness:\s*(?P<faithfulness>-?[0-9.]+)\s*\n"
    r"\s*coverage:\s*(?P<coverage>-?[0-9.]+)\s*\n"
    r"\s*clarity:\s*(?P<clarity>-?[0-9.]+)",
    re.IGNORECASE,
)


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
