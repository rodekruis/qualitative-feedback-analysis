"""Helpers for iterative LLM coding prompts and response parsing."""

import json

from pydantic import BaseModel, Field


class JudgeResponse(BaseModel):
    """Structured output returned by the LLM judge for one hierarchy level."""

    score: float = Field(description="Confidence score between 0 and 1.")
    explanation: str = Field(description="Reason for this score.")


_SYSTEM = """You are a classification agent for beneficiary feedback items.

Your task is to classify the feedback item using only the options provided at the current hierarchy level.

Goal:
Select the best-supported option(s) while balancing:
- precision: avoid clearly wrong labels
- recall: do not miss labels that are reasonably supported

Instructions:
- Use only the current-level options provided.
- Use the feedback text as the main evidence.
- Use the parent path context only to interpret the current level correctly and disambiguate meaning.
- Select an option if it is:
  - clearly supported by the feedback text, or
  - a reasonable interpretation that is strongly implied by the text
- Do not select an option if it is:
  - only loosely related,
  - a weak or doubtful match,
  - dependent on speculation beyond the text,
  - more general than what the text actually supports
- Multi-label is allowed, but only when the feedback contains multiple distinct ideas that separately support different options.
- Do not select multiple options that express the same underlying idea.
- Prefer the best-fitting option(s) rather than returning none.
- Return an empty list only when none of the options are meaningfully supported by the feedback.

Selection guidance:
- Most items should result in 1 selected option.
- Select 2 or more only when the text clearly contains multiple distinct classifiable ideas.
- Avoid broad over-selection.

Output rules:
- Output JSON only.
- Do not output markdown.
- Do not output explanations.
- Do not output any text other than the JSON object.

Return exactly this format:
{"selected":[<integer indices>]}"""

SYSTEM_PROMPT = _SYSTEM


def _build_user_message(
    *,
    feedback_text: str,
    current_level: str,
    labels: list[str],
    hierarchy_path: list[tuple[str, str]],
) -> str:
    """Format the user turn: feedback, optional path, current level, numbered options."""
    if hierarchy_path:
        path_lines = "\n".join(f"{label}: {value}" for label, value in hierarchy_path)
        path_block = f"Hierarchy path already selected:\n{path_lines}\n\n"
    else:
        path_block = ""
    options = "\n".join(f"{i}: {labels[i]}" for i in range(len(labels)))
    return (
        f"Feedback:\n---\n{feedback_text}\n---\n"
        f"{path_block}"
        f"Current level:\n{current_level}\n\n"
        f"Options:\n{options}"
    )


def _parse_selected_indices(raw: str, num_options: int) -> list[int]:
    """JSON ``{"selected": [...]}`` → unique indices in ``0 .. num_options-1``."""
    try:
        selected = json.loads(raw.strip())["selected"]
        if not isinstance(selected, list):
            return []
    except Exception:
        return []
    out: list[int] = []
    for x in selected:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < num_options:
            out.append(i)
    return list(dict.fromkeys(out))


def build_pick_messages(
    *,
    feedback_text: str,
    current_level: str,
    labels: list[str],
    hierarchy_path: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Build the system and user messages for one hierarchy-level pick."""
    if not labels:
        return SYSTEM_PROMPT, ""

    path = hierarchy_path or []
    return SYSTEM_PROMPT, _build_user_message(
        feedback_text=feedback_text,
        current_level=current_level,
        labels=labels,
        hierarchy_path=path,
    )


def parse_selected_indices(raw: str, num_options: int) -> list[int]:
    """Parse the model JSON response for one hierarchy-level pick."""
    return _parse_selected_indices(raw, num_options)


_JUDGE_SYSTEM = """You are evaluating whether a code assignment fits a feedback record.

Context:
These feedback records are collected from community members by Red Cross / Red Crescent National Societies as part of humanitarian programs. Feedback is qualitative and unstructured. It may be:
- Short or incomplete (a few words or one sentence)
- Indirect or emotionally expressed rather than explicit
- Originally written in a local language and translated
- About services, access, staff behaviour, health, safety, or community concerns

Your task:
Assess how well the assigned code label at the requested level fits the feedback record, given the full code path (Type > Category > Code) as context.

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

Scores between anchors are expected and encouraged. For example, a strong but not perfect match might be 0.85."""


def build_judge_messages(
    *,
    feedback_text: str,
    level: str,
    path: list[tuple[str, str]],
) -> tuple[str, str]:
    """Build system and user messages for a single-level judge call.

    Parameters
    ----------
    feedback_text:
        Raw text of the feedback item being coded.
    level:
        The hierarchy level being evaluated: ``"Type"``, ``"Category"``, or ``"Code"``.
    path:
        Full code path up to and including the current level, as
        ``[(level_name, label), ...]``. E.g. for the Category judge:
        ``[("Type", "Service Delivery"), ("Category", "Staff Behavior")]``.
    """
    path_lines = "\n".join(f"{name}: {label}" for name, label in path)
    user = (
        f"Feedback:\n---\n{feedback_text}\n---\n\n"
        f"Code path:\n{path_lines}\n\n"
        f"Evaluate the {level} assignment."
    )
    return _JUDGE_SYSTEM, user
