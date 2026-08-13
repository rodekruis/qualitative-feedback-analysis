"""Helpers for one-shot hierarchical coding prompts and per-level judge prompts."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator

from qfa.domain.models import CodingNode, FeedbackRecordModel
from qfa.services.prompts import build_feedback_record_envelope


def format_code_path(path: Sequence[tuple[str, str]]) -> str:
    """Render a ``(id, name)`` path as ``"Service Delivery > Staff Behavior"``."""
    return " > ".join(name for _, name in path)


@dataclass(frozen=True)
class CodePathOption:
    """One selectable option: a path from a root code down to some node.

    ``path`` holds ``(id, name)`` per level, root first. A path may stop at
    any depth — every node in the framework (not just leaves) is its own
    option, so a level-1-only or level-1+2 code can be selected directly
    when nothing more specific fits.
    """

    path: tuple[tuple[str, str], ...]

    @property
    def label(self) -> str:
        """Human-readable path, e.g. ``"Service Delivery > Staff Behavior"``."""
        return format_code_path(self.path)


def flatten_coding_nodes(
    nodes: list[CodingNode], _prefix: tuple[tuple[str, str], ...] = ()
) -> list[CodePathOption]:
    """Flatten a coding tree into one option per node, at every depth.

    Pre-order: a parent immediately precedes its own children, so related
    paths stay grouped together for the model.
    """
    options: list[CodePathOption] = []
    for node in nodes:
        path = (*_prefix, (node.id, node.name))
        options.append(CodePathOption(path=path))
        options.extend(flatten_coding_nodes(node.children, path))
    return options


class CodingResponse(BaseModel):
    """Structured output for one-shot hierarchical code selection.

    Confidence and explanation are deliberately absent: the pick step only
    chooses *which* paths are in play. A separate per-level judge call (see
    :func:`build_judge_messages`) — unchanged from the previous per-level
    pick/judge design — scores and explains each one afterwards.
    """

    selected: list[int] = Field(
        default_factory=list,
        description="Indices of the selected options from the numbered <options> list.",
    )

    @field_validator("selected", mode="before")
    @classmethod
    def _drop_unparseable_indices(cls, value: object) -> object:
        """Coerce per-element instead of failing the whole list on one bad token.

        The pick list can be long (every node at every depth), so a single
        stray non-integer element in an otherwise-good response would
        invalidate every valid index alongside it.
        """
        if not isinstance(value, list):
            return value
        coerced: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                continue
            try:
                coerced.append(int(item))
            except ValueError:
                continue
        return coerced


_SYSTEM = """You are a classification agent for feedback records from community members, collected by Red Cross / Red Crescent National Societies as part of humanitarian programs.

Task:
Select the best-fitting code(s) for the feedback record from the full coding framework given as a numbered list of options. Each option is a complete path through the hierarchy (e.g. "Service Delivery > Staff Behavior > Rudeness"); some paths end earlier than others because not every branch goes three levels deep. Prefer the deepest, most specific path available — pick a shorter, more general path only when the feedback genuinely does not support going any deeper.

Context:
Feedback is qualitative and unstructured. It may be:
- Short or incomplete (a few words or one sentence)
- Indirect or emotionally expressed rather than explicit
- Originally written in a local language and translated
- About services, access, staff behaviour, health, safety, or community concerns

Selection guidance:
- Use the feedback text as the main evidence.
- Select an option if it is clearly supported by the feedback text, or a reasonable interpretation that is strongly implied by the text.
- Do not select an option if it is only loosely related, a weak or doubtful match, dependent on speculation beyond the text, or more general than the text actually supports when a more specific path fits better.
- Multi-label is allowed, but only when the feedback contains multiple distinct ideas that separately support different paths. Do not select multiple paths that express the same underlying idea.
- Most items should result in 1 selected path. Select 2 or more only when the text clearly contains multiple distinct classifiable ideas. Avoid broad over-selection.
- There is always at least one path that is a reasonable fit for the feedback text — prefer the best-fitting option(s) rather than returning none.

Output rules:
- Output JSON only.
- Do not output markdown.
- Do not output explanations.
- For each selected path, give only its option index."""

SYSTEM_PROMPT = _SYSTEM


def build_coding_messages(
    *,
    feedback_record: FeedbackRecordModel,
    options: list[CodePathOption],
) -> tuple[str, str]:
    """Build the system and user messages for the one-shot hierarchical pick."""
    if not options:
        return SYSTEM_PROMPT, ""

    options_block = "\n".join(f"{i}: {opt.label}" for i, opt in enumerate(options))
    user_message = (
        f"{build_feedback_record_envelope(feedback_record, include_metadata=False, include_id=False)}\n"
        f"<options>\n{options_block}\n</options>"
    )
    return SYSTEM_PROMPT, user_message


class JudgeResponse(BaseModel):
    """Structured output returned by the LLM judge for one hierarchy level."""

    score: float = Field(description="Confidence score between 0 and 1.")
    explanation: str = Field(
        description="Reason for this score, in at most two sentences."
    )


_JUDGE_SYSTEM = """You are evaluating whether a code assignment fits a feedback record.

Context:
These feedback records are collected from community members by Red Cross / Red Crescent National Societies as part of humanitarian programs. Feedback is qualitative and unstructured. It may be:
- Short or incomplete (a few words or one sentence)
- Indirect or emotionally expressed rather than explicit
- Originally written in a local language and translated
- About services, access, staff behaviour, health, safety, or community concerns

Your task:
Assess how well the assigned code label at the requested level fits the feedback record, given the full code path as context.

Important:
- Do not penalise feedback for being brief or colloquial — short feedback is normal in this domain.
- Do not require exact keyword matches. Assess meaning and intent.
- A reasonable interpretation of ambiguous feedback can still warrant a high confidence score, as long as it is grounded in the text.
- Do not assign high confidence based on superficial similarity alone — the code must genuinely capture what the community member is expressing.

Scoring:
Assign a score from 0.0 to 1.0. Use the full continuous range — do not round to fixed values.

Reference anchors:
- 1.0: the feedback clearly and directly supports this assignment
- 0.75: the feedback reasonably supports this assignment
- 0.5: the assignment is plausible but uncertain
- 0.25: the fit is weak or speculative
- 0.0: the feedback does not support this assignment or the assignment is clearly wrong

Scores between anchors are expected and encouraged. For example, a strong but not perfect match might be 0.85.

Explanation:
Keep the explanation to at most two sentences."""


def build_judge_messages(
    *,
    feedback_record: FeedbackRecordModel,
    level: str,
    path: list[tuple[str, str]],
) -> tuple[str, str]:
    """Build system and user messages for a single-level judge call.

    Parameters
    ----------
    feedback_record:
        The feedback record being coded.
    level:
        The hierarchy level being evaluated: ``"Code level 1"``, ``"Code level 2"``, or ``"Code level 3"``.
    path:
        Full code path up to and including the current level, as
        ``[(level_name, label), ...]``. E.g. for the Code level 2 judge:
        ``[("Code level 1", "Service Delivery"), ("Code level 2", "Staff Behavior")]``.
    """
    path_lines = "\n".join(f"{name}: {label}" for name, label in path)
    user = (
        f"{build_feedback_record_envelope(feedback_record, include_metadata=False, include_id=False)}\n"
        f"<code_path>\n{path_lines}\n</code_path>\n\n"
        f"<instruction>\nEvaluate the {level} assignment.\n</instruction>"
    )
    return _JUDGE_SYSTEM, user
