"""Helpers for iterative LLM coding prompts and response parsing."""

import json

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
