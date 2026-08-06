"""Helpers for one-shot hierarchical coding prompts and response parsing."""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from qfa.domain.models import CodingNode, FeedbackRecordModel
from qfa.services.prompts import build_feedback_record_envelope


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
        return " > ".join(name for _, name in self.path)


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


class CodeSelection(BaseModel):
    """One selected code path with its self-reported confidence."""

    index: int = Field(
        description="Index of the selected option from the numbered <options> list."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that this path fits the feedback record (0-1).",
    )
    explanation: str = Field(
        description="Reason for this selection, in at most two sentences."
    )


class CodingResponse(BaseModel):
    """Structured output for one-shot hierarchical code selection."""

    selected: list[CodeSelection] = Field(
        default_factory=list, description="Selected code paths, in any order."
    )


_SYSTEM = """You are a classification agent for feedback records from community members, collected by Red Cross / Red Crescent National Societies as part of humanitarian programs.

Task:
Select the best-fitting code(s) for the feedback record from the full coding framework given as a numbered list of options. Each option is a complete path through the hierarchy (e.g. "Service Delivery > Staff Behavior > Rudeness"); some paths end earlier than others because not every branch goes three levels deep. Pick whichever path best captures the feedback, regardless of its depth — a shorter, more general path is a valid choice when nothing more specific fits.

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

Confidence scoring:
For each path you select, assign a score from 0.0 to 1.0 reflecting how well it fits, using the full continuous range — do not round to fixed values:
- 1.0: the feedback clearly and directly supports this path
- 0.75: the feedback reasonably supports this path
- 0.5: the path is plausible but uncertain
- 0.25: the fit is weak or speculative
- 0.0: the feedback does not support this path
Scores between anchors are expected and encouraged. For example, a strong but not perfect match might be 0.85.

Output rules:
- Output JSON only.
- Do not output markdown.
- Do not output explanations outside the JSON.
- For each selected path, give its option index, a confidence score, and an explanation of at most two sentences."""

SYSTEM_PROMPT = _SYSTEM


def build_coding_messages(
    *,
    feedback_record: FeedbackRecordModel,
    options: list[CodePathOption],
) -> tuple[str, str]:
    """Build the system and user messages for one-shot hierarchical coding."""
    if not options:
        return SYSTEM_PROMPT, ""

    options_block = "\n".join(f"{i}: {opt.label}" for i, opt in enumerate(options))
    user_message = (
        f"{build_feedback_record_envelope(feedback_record, include_metadata=False, include_id=False)}\n"
        f"<options>\n{options_block}\n</options>"
    )
    return SYSTEM_PROMPT, user_message
