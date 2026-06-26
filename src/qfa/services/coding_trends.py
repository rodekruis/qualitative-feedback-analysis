"""Deterministic, non-LLM coding-trend table.

Counts coding labels over time periods, assembled from feedback-record
metadata. Best-effort: when the configured date field is absent the
table is omitted (``None``) and the reduce step degrades to text-only
synthesis. No LLM, no port — pure ``services`` logic.

The period granularity is configurable (``day`` / ``week`` / ``month``)
so a one-month corpus can still show meaningful trend buckets. ``week``
uses ISO week numbering (``YYYY-Www``) so the ISO year — not the
calendar year — anchors the bucket, avoiding the silent off-by-one
where 2024-12-30 would otherwise collide with truly-January-2024
records.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import date

from qfa.domain.clustering_models import (
    CodingTrendCell,
    CodingTrendTable,
    TrendPeriod,
)
from qfa.domain.models import FeedbackRecordModel

logger = logging.getLogger(__name__)

# Re-exported here for back-compat with call sites that import the alias
# from ``qfa.services.coding_trends`` (where the bucketing logic lives).
__all__ = [
    "TrendPeriod",
    "build_coding_trend_table",
    "render_coding_trend_table",
]


def _period_of(raw_date: object, period: TrendPeriod) -> str | None:
    """Return the ``period``-bucket label for an ISO-8601-ish date string.

    Best-effort parsing of the leading date portion:

    - ``month`` → ``YYYY-MM`` (only needs ``YYYY-MM`` to be present).
    - ``day``   → ``YYYY-MM-DD`` (needs a full date).
    - ``week``  → ``YYYY-Www`` using ISO week numbering (needs a full date).

    Returns ``None`` when the value is not a parseable date prefix.

    Parameters
    ----------
    raw_date : object
        Metadata value at the configured ``date_field``. Anything that
        isn't a string returns ``None``; strings are parsed leniently
        from their leading characters so ``"2024-01-05T10:00:00Z"`` works
        the same as ``"2024-01-05"``.
    period : TrendPeriod
        Granularity to bucket into.
    """
    if not isinstance(raw_date, str):
        return None
    text = raw_date.strip()
    # Month only needs YYYY-MM; day/week need the full YYYY-MM-DD prefix.
    if period == "month":
        if len(text) < 7 or text[4] != "-":
            return None
        year, month = text[:4], text[5:7]
        if not (year.isdigit() and month.isdigit()):
            return None
        return f"{year}-{month}"

    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        return None
    year, month, day = text[:4], text[5:7], text[8:10]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return None
    if period == "day":
        return f"{year}-{month}-{day}"
    # week: ISO calendar — iso_year, not calendar year, is what we bucket on
    # (so late-December dates land in the correct ISO year).
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return None
    iso_year, iso_week, _ = parsed.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _codes_in_record(
    record: FeedbackRecordModel, code_fields: Sequence[str]
) -> list[str]:
    """Extract coding labels from a record's metadata.

    Each configured code field may hold a comma-separated string of
    labels (matching the corpus convention). Empty/missing fields
    contribute nothing.

    Parameters
    ----------
    record : FeedbackRecordModel
        The record whose metadata is inspected. Only metadata is read;
        the record's text is not used.
    code_fields : Sequence[str]
        Metadata keys to inspect, in order. Non-string values and
        missing keys are silently skipped — a code field that is
        absent on a particular record contributes nothing rather than
        raising.

    Returns
    -------
    list[str]
        The flat list of labels harvested across ``code_fields``,
        stripped of whitespace and with empty entries removed. The
        order matches the order of ``code_fields`` and, within each
        field, the order of the comma-separated values.
    """
    labels: list[str] = []
    for field in code_fields:
        raw = getattr(record.metadata, field, None)
        if not isinstance(raw, str):
            continue
        labels.extend(c.strip() for c in raw.split(",") if c.strip())
    return labels


def build_coding_trend_table(
    records: tuple[FeedbackRecordModel, ...],
    *,
    date_field: str,
    code_fields: Sequence[str],
    period: TrendPeriod = "week",
) -> CodingTrendTable | None:
    """Build a code-by-period count table from record metadata.

    Parameters
    ----------
    records : tuple[FeedbackRecordModel, ...]
        The full input record set.
    date_field : str
        Metadata key holding the record's date (parsed to a ``period``
        bucket label per :func:`_period_of`).
    code_fields : Sequence[str]
        Metadata keys holding coding labels (comma-separated strings).
    period : TrendPeriod
        Bucket granularity. ``week`` (the default) is usually right;
        ``month`` is better for multi-year corpora; ``day`` for
        short-window deep-dives.

    Returns
    -------
    CodingTrendTable | None
        The assembled table, or ``None`` when no record carries a
        parseable date in ``date_field`` (best-effort omission).
    """
    counter: Counter[tuple[str, str]] = Counter()
    periods: set[str] = set()

    for record in records:
        bucket = _period_of(getattr(record.metadata, date_field, None), period)
        if bucket is None:
            continue
        periods.add(bucket)
        for code in _codes_in_record(record, code_fields):
            counter[(code, bucket)] += 1

    if not periods:
        logger.warning(
            "coding_trends: date field %r matched 0 of %d record(s) — "
            "check if the field name is exactly the same and if the values are parseable dates",
            date_field,
            len(records),
        )
        return None

    if not counter:
        logger.warning(
            "coding_trends: code fields %r matched 0 labels across %d dated record(s) — "
            "check code fields names and check if the values are comma-separated strings",
            list(code_fields),
            len(periods),
        )

    cells = tuple(
        CodingTrendCell(code=code, period=bucket, count=count)
        for (code, bucket), count in sorted(counter.items())
    )
    return CodingTrendTable(periods=tuple(sorted(periods)), cells=cells)


def render_coding_trend_table(table: CodingTrendTable) -> str:
    """Render the table as a compact text grid for the reduce prompt.

    Rows are codes, columns are periods, cells are integer counts. This
    is the faithfulness anchor the synthesis prompt cites.
    """
    codes = sorted({cell.code for cell in table.cells})
    lookup = {(cell.code, cell.period): cell.count for cell in table.cells}
    header = "code," + ",".join(table.periods)
    lines = [header]
    for code in codes:
        row = [code] + [str(lookup.get((code, p), 0)) for p in table.periods]
        lines.append(",".join(row))
    return "\n".join(lines)
