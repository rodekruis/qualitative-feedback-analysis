"""Tests for the map (leaf) and reduce (synthesis) prompt builders.

Why: the guardrails contract (#75/#117 — records are data, not
instructions) MUST be present at BOTH the map and reduce prompts, and the
reduce prompt must carry the deterministic coding-trend table as a
faithfulness anchor. These are the security- and faithfulness-load-bearing
properties of the hierarchical path.
"""

from qfa.domain.clustering_models import CodingTrendCell, CodingTrendTable
from qfa.services.hierarchical_prompts import (
    build_map_system_message,
    build_reduce_system_message,
    build_reduce_user_message,
)
from qfa.services.prompts import ANALYZE_GUARDRAILS_PROMPT


def test_map_system_message_contains_guardrails() -> None:
    """The map (leaf) system message embeds the analyse guardrails verbatim.

    Why: the leaf prompt is where raw records appear — the focal point for
    injection defence.
    """
    assert ANALYZE_GUARDRAILS_PROMPT in build_map_system_message()


def test_reduce_system_message_contains_guardrails() -> None:
    """The reduce (synthesis) system message also embeds the guardrails.

    Why: partial analyses are still untrusted derived content; the spec
    requires guardrails at the reduce level too.
    """
    assert ANALYZE_GUARDRAILS_PROMPT in build_reduce_system_message()


def test_reduce_user_message_includes_partials_and_trend_table() -> None:
    """The reduce user message embeds every partial analysis and the trend table.

    Why: synthesis must combine all leaf partials, and the trend table is
    the independent non-LLM anchor the prose is checked against.
    """
    table = CodingTrendTable(
        periods=("2024-01",),
        cells=(CodingTrendCell(code="Water", period="2024-01", count=4),),
    )
    msg = build_reduce_user_message(
        analyst_prompt="What are the trends?",
        partial_analyses=("partial one", "partial two"),
        trend_table=table,
    )
    assert "partial one" in msg
    assert "partial two" in msg
    assert "Water" in msg  # rendered trend table present
    assert "What are the trends?" in msg


def test_reduce_user_message_without_trend_table_degrades_gracefully() -> None:
    """A ``None`` trend table yields text-only synthesis, no crash, no table block.

    Why: best-effort — corpora without date/code metadata still produce a
    valid reduce prompt.
    """
    msg = build_reduce_user_message(
        analyst_prompt="What are the trends?",
        partial_analyses=("only partial",),
        trend_table=None,
    )
    assert "only partial" in msg
