"""Iterative LLM coding: one model call per level of the coding framework tree."""

import json
import logging
from typing import Any

from qfa.domain.models import LLMResponse
from qfa.domain.ports import LLMPort

logger = logging.getLogger(__name__)


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


async def pick(
    llm: LLMPort,
    *,
    feedback_text: str,
    current_level: str,
    labels: list[str],
    tenant_id: str,
    timeout: float,
    hierarchy_path: list[tuple[str, str]] | None = None,
) -> list[int]:
    """Ask the LLM which options apply at one level of the coding hierarchy.

    Builds a user message with the feedback text, optional parent path, the current
    level name, and a numbered list of ``labels``; the model must answer with JSON
    ``{"selected": [<indices>]}``. Invalid or unparsable output is treated as an
    empty selection (no exception).

    Parameters
    ----------
    llm : LLMPort
        LLM adapter used for the completion call.
    feedback_text : str
        Full beneficiary feedback text being coded (included in every pick).
    current_level : str
        Name of the level being decided (e.g. ``"Frames"``, ``"Codes"``), shown after
        ``Current level:`` in the user message.
    labels : list[str]
        Human-readable option strings for this level, in tree order; index ``i`` in
        the prompt matches child ``i`` in the framework structure.
    tenant_id : str
        Tenant identifier passed through to the LLM provider.
    timeout : float
        Maximum time in seconds for this completion request.
    hierarchy_path : list[tuple[str, str]] | None
        Ordered parent context as ``(segment label, chosen name)``, e.g.
        ``("Frame", "COVID-19"), ("Type", "Observation, perception or belief")``.
        Omitted or empty when at the root level.

    Returns
    -------
    list[int]
        Zero-based indices of selected options. Empty if ``labels`` is empty, if the
        model output is not valid JSON with a list ``selected``, or if list entries
        are not valid in-range indices.
    """
    if not labels:
        return []

    path = hierarchy_path or []
    user_message = _build_user_message(
        feedback_text=feedback_text,
        current_level=current_level,
        labels=labels,
        hierarchy_path=path,
    )

    response: LLMResponse = await llm.complete(
        _SYSTEM, user_message, timeout, tenant_id
    )

    return _parse_selected_indices(response.text, len(labels))


async def classify_feedback(
    llm: LLMPort,
    *,
    feedback_text: str,
    framework: dict[str, Any],
    tenant_id: str,
    timeout: float,
    max_codes: int,
) -> list[tuple[str, str]]:
    """Assign leaf codes by walking the framework tree with repeated ``pick`` calls.

    Traverses ``coding_frames`` → ``types`` → ``categories`` → ``codes``. At each
    level the LLM may select zero or more children; selections fan out until leaf
    codes are collected. Stops early once ``max_codes`` assignments are stored.

    Parameters
    ----------
    llm : LLMPort
        LLM adapter used for each ``pick``.
    feedback_text : str
        Feedback text to classify.
    framework : dict[str, Any]
        Coding framework payload, typically matching the API ``coding_framework``
        field. Must contain key ``coding_frames``: a list of frame objects; each
        frame has ``name``, ``types``; each type has ``categories``; each category
        has ``codes`` with at least ``code_id`` and ``name`` on leaf entries.
    tenant_id : str
        Tenant identifier passed to the LLM on every completion.
    timeout : float
        Per-completion timeout in seconds (passed to ``pick`` / ``LLMPort.complete``).
    max_codes : int
        Maximum number of leaf codes to return; traversal stops once this many
        are collected.

    Returns
    -------
    list[tuple[str, str]]
        Each tuple is ``(code_id, code_label)`` from the leaf ``codes`` entry.
    """
    assigned_codes: list[tuple[str, str]] = []
    coding_frames = framework.get("coding_frames") or []

    # Level 1: Coding frames (e.g. COVID-19 vs drought vs poverty) TODO: Currently implemented, will be removed in future because known.
    frame_labels = [str(frame.get("name", "")) for frame in coding_frames]
    selected_frame_indices = await pick(
        llm,
        feedback_text=feedback_text,
        current_level="Frames",
        labels=frame_labels,
        tenant_id=tenant_id,
        timeout=timeout,
        hierarchy_path=None,
    )

    for frame_index in selected_frame_indices:
        frame = coding_frames[frame_index]
        frame_name = str(frame.get("name", ""))
        types = frame.get("types") or []

        # Level 2: types (e.g. 'Encouragement or praise' vs 'Question').
        type_labels = [str(type_entry.get("name", "")) for type_entry in types]
        selected_type_indices = await pick(
            llm,
            feedback_text=feedback_text,
            current_level="Types",
            labels=type_labels,
            tenant_id=tenant_id,
            timeout=timeout,
            hierarchy_path=[("Frame", frame_name)],
        )

        for type_index in selected_type_indices:
            type_entry = types[type_index]
            type_name = str(type_entry.get("name", ""))
            categories = type_entry.get("categories") or []

            # Level 3: categories (e.g. 'Questions about disease outbreak' vs 'Statement of thanks').
            category_labels = [str(category.get("name", "")) for category in categories]
            selected_category_indices = await pick(
                llm,
                feedback_text=feedback_text,
                current_level="Categories",
                labels=category_labels,
                tenant_id=tenant_id,
                timeout=timeout,
                hierarchy_path=[("Frame", frame_name), ("Type", type_name)],
            )

            for category_index in selected_category_indices:
                category = categories[category_index]
                category_name = str(category.get("name", ""))
                codes = category.get("codes") or []

                # Level 4: actual assignable codes
                code_labels = [str(code.get("name", "")) for code in codes]
                selected_code_indices = await pick(
                    llm,
                    feedback_text=feedback_text,
                    current_level="Codes",
                    labels=code_labels,
                    tenant_id=tenant_id,
                    timeout=timeout,
                    hierarchy_path=[
                        ("Frame", frame_name),
                        ("Type", type_name),
                        ("Category", category_name),
                    ],
                )

                for code_index in selected_code_indices:
                    code = codes[code_index]
                    assigned_codes.append(
                        (
                            str(code.get("code_id", "")),
                            str(code.get("name", "")),
                        )
                    )
                    if len(assigned_codes) >= max_codes:
                        return assigned_codes

    return assigned_codes
