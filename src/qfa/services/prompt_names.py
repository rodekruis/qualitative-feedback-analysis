"""Prompt name constants and the ``PromptRef`` lookup used for trace linking.

Kept separate from :mod:`qfa.services.prompt_registry`: that module imports
every prompt-owning service module to build
:data:`~qfa.services.prompt_registry.SYSTEM_PROMPTS`, so a service importing
it back would create a cycle. Call sites import their prompt's name
constant and :func:`prompt_ref` from here instead, and
:mod:`qfa.services.prompt_registry` imports the same constants as its
dict keys, so a name is spelled once no matter which side reads it.
"""

from collections.abc import Mapping

from qfa.domain.models import PromptRef

ANALYZE_SINGLE_PASS_SYSTEM = "analyze-single-pass-system"  # noqa: S105 (not a password)
ANALYZE_HIERARCHICAL_MAP_SYSTEM = "analyze-hierarchical-map-system"
ANALYZE_HIERARCHICAL_REDUCE_SYSTEM = "analyze-hierarchical-reduce-system"
ANALYZE_JUDGE = "analyze-judge"
SUMMARIZE_AGGREGATE_SYSTEM = "summarize-aggregate-system"
SUMMARIZE_SINGLE_SYSTEM = "summarize-single-system"
SUMMARIZE_COMMUNITY_MEETING_SYSTEM = "summarize-community-meeting-system"
SUMMARIZE_JUDGE = "summarize-judge"
CODING_CLASSIFIER_SYSTEM = "coding-classifier-system"
CODING_CLASSIFIER_JUDGE = "coding-classifier-judge"
SENSITIVITY_DETECTION_SYSTEM = "sensitivity-detection-system"


def prompt_ref(versions: Mapping[str, int], name: str) -> PromptRef | None:
    """Look up ``name``'s current Langfuse version in ``versions``.

    Returns ``None`` when Langfuse is unconfigured or that name's push
    failed, so a call site can pass the result straight to
    ``LLMPort.complete``'s ``prompt`` keyword without a branch.
    """
    version = versions.get(name)
    return PromptRef(name=name, version=version) if version is not None else None
