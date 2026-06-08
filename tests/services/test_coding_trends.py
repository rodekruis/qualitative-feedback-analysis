"""Tests for the deterministic coding-trend table.

Why: the table is the non-LLM faithfulness anchor in the reduce prompt —
its counts must be exact and reproducible, and it must degrade to
``None`` (not error) when metadata is missing, per the best-effort
decision in the design spec.
"""

from qfa.domain.clustering_models import CodingTrendCell, CodingTrendTable
from qfa.domain.models import FeedbackRecordModel
from qfa.services.coding_trends import (
    build_coding_trend_table,
    render_coding_trend_table,
)


def _record(rec_id: str, created: str, codes: str) -> FeedbackRecordModel:
    return FeedbackRecordModel(
        id=rec_id,
        text="some feedback",
        metadata={"created": created, "codes": codes},
    )


def test_counts_codes_per_month_period() -> None:
    """Codes are counted per month bucket derived from the date field.

    Why: longitudinal questions ("how did rumours evolve?") need exact
    per-period counts a QFA user can verify independently of the LLM.
    Monthly buckets are one of three supported granularities (the
    others being ``week`` and ``day``); this test pins the month path.
    """
    records = (
        _record("r1", "2024-01-05T10:00:00Z", "Water,Health"),
        _record("r2", "2024-01-20T10:00:00Z", "Water"),
        _record("r3", "2024-02-02T10:00:00Z", "Water"),
    )
    table = build_coding_trend_table(
        records,
        date_field="created",
        code_fields=("codes",),
        period="month",
    )
    assert table is not None
    assert table.periods == ("2024-01", "2024-02")
    counts = {(c.code, c.period): c.count for c in table.cells}
    assert counts[("Water", "2024-01")] == 2
    assert counts[("Health", "2024-01")] == 1
    assert counts[("Water", "2024-02")] == 1


def test_counts_codes_per_week_period_default() -> None:
    """Week is the default granularity; ISO week labels are ``YYYY-Www``.

    Why: typical operational corpora cover 1-3 months; weekly buckets
    are the right granularity to surface trend signal without
    overwhelming the reduce prompt. ISO week labelling avoids the
    last-week-of-December cross-year off-by-one (2024-12-30 is ISO week
    1 of 2025), so the assertion below uses the ISO year.
    """
    records = (
        # 2024-01-01 is a Monday — ISO week 2024-W01.
        _record("r1", "2024-01-01T10:00:00Z", "Water"),
        # 2024-01-07 is the Sunday of the same ISO week (2024-W01).
        _record("r2", "2024-01-07T10:00:00Z", "Water"),
        # 2024-01-08 is the Monday of the next ISO week (2024-W02).
        _record("r3", "2024-01-08T10:00:00Z", "Water"),
    )
    table = build_coding_trend_table(
        records, date_field="created", code_fields=("codes",)
    )
    assert table is not None
    assert table.periods == ("2024-W01", "2024-W02")
    counts = {(c.code, c.period): c.count for c in table.cells}
    assert counts[("Water", "2024-W01")] == 2
    assert counts[("Water", "2024-W02")] == 1


def test_counts_codes_per_day_period() -> None:
    """Daily buckets use ``YYYY-MM-DD`` labels.

    Why: short-window deep-dives (a single operation day) need the
    finest granularity. Confirms the day branch parses the full date
    prefix correctly.
    """
    records = (
        _record("r1", "2024-01-05T10:00:00Z", "Water"),
        _record("r2", "2024-01-05T22:00:00Z", "Water"),
        _record("r3", "2024-01-06T08:00:00Z", "Water"),
    )
    table = build_coding_trend_table(
        records, date_field="created", code_fields=("codes",), period="day"
    )
    assert table is not None
    counts = {(c.code, c.period): c.count for c in table.cells}
    assert counts[("Water", "2024-01-05")] == 2
    assert counts[("Water", "2024-01-06")] == 1


def test_returns_none_when_date_field_absent() -> None:
    """No date metadata → table omitted (None), reduce degrades to text-only.

    Why: the spec mandates best-effort; a missing field must never raise.
    """
    records = (FeedbackRecordModel(id="r1", text="x", metadata={"codes": "Water"}),)
    table = build_coding_trend_table(
        records, date_field="created", code_fields=("codes",)
    )
    assert table is None


def test_records_without_codes_are_skipped_not_errored() -> None:
    """Records missing the code field contribute no cells but do not raise.

    Why: corpora are heterogeneous; partial metadata must degrade
    gracefully rather than fail the whole request.
    """
    records = (
        _record("r1", "2024-01-05T10:00:00Z", "Water"),
        FeedbackRecordModel(
            id="r2", text="x", metadata={"created": "2024-01-06T10:00:00Z"}
        ),
    )
    table = build_coding_trend_table(
        records, date_field="created", code_fields=("codes",), period="month"
    )
    assert table is not None
    counts = {(c.code, c.period): c.count for c in table.cells}
    assert counts == {("Water", "2024-01"): 1}


def test_render_produces_code_by_period_grid() -> None:
    """Rendering yields a CSV-like grid with zero-filled missing cells.

    Why: the reduce prompt embeds this verbatim; a stable, fully-filled
    grid lets the LLM (and a human reviewer) read counts unambiguously.
    """
    table = CodingTrendTable(
        periods=("2024-01", "2024-02"),
        cells=(
            CodingTrendCell(code="Water", period="2024-01", count=2),
            CodingTrendCell(code="Health", period="2024-02", count=1),
        ),
    )
    rendered = render_coding_trend_table(table)
    assert rendered.splitlines()[0] == "code,2024-01,2024-02"
    assert "Water,2,0" in rendered
    assert "Health,0,1" in rendered
