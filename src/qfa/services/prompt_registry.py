"""The one registry of every hardcoded system prompt this app versions in Langfuse.

:data:`SYSTEM_PROMPTS` is what
:func:`qfa.api.composition.build_prompt_versions` pushes to Langfuse at
startup, and what the tests check against. Adding a twelfth prompt later
means adding one entry here — every value is imported from its owning
module, never copied as a duplicate literal, so this registry cannot drift
from the text an LLM call actually sends.

Each of the 11 names maps to one combined, static prompt (see SPEC.md
section 3 for the full inventory). Two rules for what is versioned:

* Where a real call site appends request-specific text to a prompt — the
  output-language instruction, or ``summarize_bulk``'s free-text
  ``request.prompt`` override — only the static part is versioned here. The
  dynamic suffix is specific to one request, not part of the prompt's
  identity.
* A prompt written as a Python ``.format()`` template (with placeholders
  such as ``{source_text}``) is pushed unfilled, as plain text. Langfuse
  never renders these templates, so its own ``{{mustache}}`` syntax is never
  used here.
"""

from qfa.services.coding_classifier import _JUDGE_SYSTEM, SYSTEM_PROMPT
from qfa.services.hierarchical_prompts import _MAP_ACTION_PROMPT, _REDUCE_ACTION_PROMPT
from qfa.services.prompts import (
    ANALYZE_ACTION_PROMPT,
    ANALYZE_GUARDRAILS_PROMPT,
    ANALYZE_JUDGE_PROMPT,
    ANALYZE_SYSTEM_PROMPT,
)
from qfa.services.sensitivity import _DEFAULT_SENSITIVITY_DETECTION_PROMPT
from qfa.services.summarize import (
    _DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT,
    _DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT,
    _DEFAULT_SUMMARIZATION_PROMPT,
    _JUDGE_PROMPT,
)

# Composed exactly as their real call sites assemble them (analyze.py's
# analyze_bulk, hierarchical_prompts.py's build_map_system_message /
# build_reduce_system_message), minus the output-language suffix those call
# sites append per request.
_ANALYZE_SINGLE_PASS_SYSTEM = (
    f"{ANALYZE_SYSTEM_PROMPT}\n\n{ANALYZE_GUARDRAILS_PROMPT}\n\n{ANALYZE_ACTION_PROMPT}"
)
_ANALYZE_HIERARCHICAL_MAP_SYSTEM = (
    f"{ANALYZE_SYSTEM_PROMPT}\n\n{ANALYZE_GUARDRAILS_PROMPT}\n\n{_MAP_ACTION_PROMPT}"
)
_ANALYZE_HIERARCHICAL_REDUCE_SYSTEM = (
    f"{ANALYZE_SYSTEM_PROMPT}\n\n{ANALYZE_GUARDRAILS_PROMPT}\n\n{_REDUCE_ACTION_PROMPT}"
)

SYSTEM_PROMPTS: dict[str, str] = {
    "analyze-single-pass-system": _ANALYZE_SINGLE_PASS_SYSTEM,
    "analyze-hierarchical-map-system": _ANALYZE_HIERARCHICAL_MAP_SYSTEM,
    "analyze-hierarchical-reduce-system": _ANALYZE_HIERARCHICAL_REDUCE_SYSTEM,
    "analyze-judge": ANALYZE_JUDGE_PROMPT,
    "summarize-aggregate-system": _DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT,
    "summarize-single-system": _DEFAULT_SUMMARIZATION_PROMPT,
    "summarize-community-meeting-system": (
        _DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT
    ),
    "summarize-judge": _JUDGE_PROMPT,
    "coding-classifier-system": SYSTEM_PROMPT,
    "coding-classifier-judge": _JUDGE_SYSTEM,
    "sensitivity-detection-system": _DEFAULT_SENSITIVITY_DETECTION_PROMPT,
}
