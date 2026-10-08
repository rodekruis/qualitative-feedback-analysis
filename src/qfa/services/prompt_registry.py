"""The one registry of every hardcoded system prompt this app versions in Langfuse.

:data:`SYSTEM_PROMPTS` is what
:func:`qfa.api.composition.build_prompt_versions` pushes to Langfuse at
startup, and what the tests check against. Adding a twelfth prompt later
means adding one entry here — every value is imported from its owning
module, never copied as a duplicate literal, so this registry cannot drift
from the text an LLM call actually sends.

Each of the 11 names maps to one combined, static prompt. Two rules for
what is versioned:

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
from qfa.services.hierarchical_prompts import (
    build_map_system_message,
    build_reduce_system_message,
)
from qfa.services.prompt_names import (
    ANALYZE_HIERARCHICAL_MAP_SYSTEM,
    ANALYZE_HIERARCHICAL_REDUCE_SYSTEM,
    ANALYZE_JUDGE,
    ANALYZE_SINGLE_PASS_SYSTEM,
    CODING_CLASSIFIER_JUDGE,
    CODING_CLASSIFIER_SYSTEM,
    SENSITIVITY_DETECTION_JUDGE,
    SENSITIVITY_DETECTION_SYSTEM,
    SUMMARIZE_AGGREGATE_SYSTEM,
    SUMMARIZE_COMMUNITY_MEETING_SYSTEM,
    SUMMARIZE_JUDGE,
    SUMMARIZE_SINGLE_SYSTEM,
)
from qfa.services.prompts import ANALYZE_JUDGE_PROMPT, build_single_pass_system_message
from qfa.services.sensitivity import (
    _DEFAULT_SENSITIVITY_DETECTION_PROMPT,
)
from qfa.services.sensitivity import (
    _JUDGE_PROMPT as _SENSITIVITY_JUDGE_PROMPT,
)
from qfa.services.summarize import (
    _DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT,
    _DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT,
    _DEFAULT_SUMMARIZATION_PROMPT,
    _JUDGE_PROMPT,
)

# Built by the same functions the real call sites use, with
# output_language=None so each builder's per-request suffix is left out.
_ANALYZE_SINGLE_PASS_SYSTEM = build_single_pass_system_message(output_language=None)
_ANALYZE_HIERARCHICAL_MAP_SYSTEM = build_map_system_message(output_language=None)
_ANALYZE_HIERARCHICAL_REDUCE_SYSTEM = build_reduce_system_message(output_language=None)

SYSTEM_PROMPTS: dict[str, str] = {
    ANALYZE_SINGLE_PASS_SYSTEM: _ANALYZE_SINGLE_PASS_SYSTEM,
    ANALYZE_HIERARCHICAL_MAP_SYSTEM: _ANALYZE_HIERARCHICAL_MAP_SYSTEM,
    ANALYZE_HIERARCHICAL_REDUCE_SYSTEM: _ANALYZE_HIERARCHICAL_REDUCE_SYSTEM,
    ANALYZE_JUDGE: ANALYZE_JUDGE_PROMPT,
    SUMMARIZE_AGGREGATE_SYSTEM: _DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT,
    SUMMARIZE_SINGLE_SYSTEM: _DEFAULT_SUMMARIZATION_PROMPT,
    SUMMARIZE_COMMUNITY_MEETING_SYSTEM: (
        _DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT
    ),
    SUMMARIZE_JUDGE: _JUDGE_PROMPT,
    CODING_CLASSIFIER_SYSTEM: SYSTEM_PROMPT,
    CODING_CLASSIFIER_JUDGE: _JUDGE_SYSTEM,
    SENSITIVITY_DETECTION_SYSTEM: _DEFAULT_SENSITIVITY_DETECTION_PROMPT,
    SENSITIVITY_DETECTION_JUDGE: _SENSITIVITY_JUDGE_PROMPT,
}
