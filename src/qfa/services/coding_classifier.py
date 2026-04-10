"""Iterative LLM coding: one model call per level of the coding framework tree."""

import json
from typing import Any

from qfa.domain.models import LLMResponse
from qfa.domain.ports import LLMPort

# Shared instructions: model must answer with indices only so we can descend the tree
# without fuzzy string matching on labels.
_SYSTEM = (
    "Classify beneficiary feedback into the given options. "
    "Select all that apply (zero or more). "
    'Reply with JSON only: {"selected":[<integer indices>]}. No markdown.'
)


async def pick(
    llm: LLMPort,
    *,
    feedback_text: str,
    hierarchy_level: str,
    labels: list[str],
    tenant_id: str,
    timeout: float,
) -> list[int]:
    """Ask the LLM which options apply at one level of the coding hierarchy.

    Builds a user message with the feedback text and a numbered list of ``labels``;
    the model must answer with JSON ``{"selected": [<indices>]}``. Invalid or
    unparsable output is treated as an empty selection (no exception).

    Parameters
    ----------
    llm : LLMPort
        LLM adapter used for the completion call.
    feedback_text : str
        Full beneficiary feedback text being coded (included in every pick).
    hierarchy_level : str
        Short label for this step, shown in the user message before the option list
        (e.g. ``"Frames"``, ``"Types"``); a colon is appended when formatting the prompt.
    labels : list[str]
        Human-readable option strings for this level, in tree order; index ``i`` in
        the prompt matches child ``i`` in the framework structure.
    tenant_id : str
        Tenant identifier passed through to the LLM provider.
    timeout : float
        Maximum time in seconds for this completion request.

    Returns
    -------
    list[int]
        Zero-based indices of selected options. Empty if ``labels`` is empty, if the
        model output is not valid JSON with a list ``selected``, or if list entries
        are not convertible to integers.
    """
    # Nothing to choose from; skip an LLM call.
    if not labels:
        return []

    # Prefix each option with its list index so the model returns integers we can map back.
    option_lines = "\n".join(
        f"{option_index}: {labels[option_index]}" for option_index in range(len(labels))
    )
    # Full user turn: original feedback plus this level's numbered options.
    user_message = (
        f"Feedback:\n---\n{feedback_text}\n---\n\n{hierarchy_level}:\n{option_lines}"
    )

    response: LLMResponse = await llm.complete(
        _SYSTEM, user_message, timeout, tenant_id
    )

    # Expect strict JSON: {"selected": [<int>, ...]}. Anything else → empty selection.
    try:
        selected = json.loads(response.text.strip())["selected"]
        if not isinstance(selected, list):
            return []
        return [int(entry) for entry in selected]
    except Exception:
        # Malformed JSON, missing key, wrong type, or non-numeric list entries.
        return []


async def classify_feedback(
    llm: LLMPort,
    *,
    feedback_text: str,
    framework: dict[str, Any],
    tenant_id: str,
    timeout: float,
    max_codes: int,
) -> list[tuple[str, str, str]]:
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
    list[tuple[str, str, str]]
        Each tuple is ``(code_id, code_label, explanation)``. ``code_id`` and
        ``code_label`` come from the leaf ``codes`` entry; ``explanation`` is
        currently always an empty string.
    """
    assigned_codes: list[tuple[str, str, str]] = []
    coding_frames = framework.get("coding_frames") or []

    # Level 1: Coding frames (e.g. COVID-19 vs drought vs poverty)
    frame_labels = [str(frame.get("name", "")) for frame in coding_frames]
    selected_frame_indices = await pick(
        llm,
        feedback_text=feedback_text,
        hierarchy_level="Frames",
        labels=frame_labels,
        tenant_id=tenant_id,
        timeout=timeout,
    )

    for frame_index in selected_frame_indices:
        if frame_index < 0 or frame_index >= len(coding_frames):
            continue
        frame = coding_frames[frame_index]
        types = frame.get("types") or []

        # Level 2: types (e.g. 'Encouragement or praise' vs 'Question').
        type_labels = [str(type_entry.get("name", "")) for type_entry in types]
        selected_type_indices = await pick(
            llm,
            feedback_text=feedback_text,
            hierarchy_level="Types",
            labels=type_labels,
            tenant_id=tenant_id,
            timeout=timeout,
        )

        for type_index in selected_type_indices:
            if type_index < 0 or type_index >= len(types):
                continue
            type_entry = types[type_index]
            categories = type_entry.get("categories") or []

            # Level 3: categories (e.g. 'Questions about disease outbreak' vs 'Statement of thanks').
            category_labels = [str(category.get("name", "")) for category in categories]
            selected_category_indices = await pick(
                llm,
                feedback_text=feedback_text,
                hierarchy_level="Categories",
                labels=category_labels,
                tenant_id=tenant_id,
                timeout=timeout,
            )

            for category_index in selected_category_indices:
                if category_index < 0 or category_index >= len(categories):
                    continue
                category = categories[category_index]
                codes = category.get("codes") or []

                # Level 4: actual assignable codes (e.g. 'Question about the number of cases and geographic areas affected ' vs 'Questions about contract tracing').
                code_labels = [str(code.get("name", "")) for code in codes]
                selected_code_indices = await pick(
                    llm,
                    feedback_text=feedback_text,
                    hierarchy_level="Codes",
                    labels=code_labels,
                    tenant_id=tenant_id,
                    timeout=timeout,
                )

                for code_index in selected_code_indices:
                    if code_index < 0 or code_index >= len(codes):
                        continue
                    code = codes[code_index]
                    assigned_codes.append(
                        (
                            str(code.get("code_id", "")),
                            str(code.get("name", "")),
                            "",
                        )
                    )
                    # Hard cap for the API contract; stop traversing once enough codes are kept.
                    if len(assigned_codes) >= max_codes:
                        return assigned_codes

    return assigned_codes
