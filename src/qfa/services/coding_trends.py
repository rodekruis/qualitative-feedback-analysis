"""Deterministic, non-LLM coding-trend table.

Counts coding labels over time periods, assembled from feedback-record
metadata. Best-effort: when the configured date field is absent the
table is omitted (``None``) and the reduce step degrades to text-only
synthesis. No LLM, no port — pure ``services`` logic.
"""

from collections import Counter
from collections.abc import Sequence

from qfa.domain.clustering_models import CodingTrendCell, CodingTrendTable
from qfa.domain.models import FeedbackRecordModel


def _period_of(raw_date: object) -> str | None:
    """Return the ``YYYY-MM`` bucket for an ISO-8601-ish date string.

    Best-effort: parses the leading ``YYYY-MM`` of a string. Returns
    ``None`` when the value is not a parseable date prefix.
    """
    if not isinstance(raw_date, str):
        return None
    text = raw_date.strip()
    # Expect at least "YYYY-MM"; reject anything shorter or malformed.
    if len(text) < 7 or text[4] != "-":
        return None
    year, month = text[:4], text[5:7]
    if not (year.isdigit() and month.isdigit()):
        return None
    return f"{year}-{month}"


def _codes_in_record(
    record: FeedbackRecordModel, code_fields: Sequence[str]
) -> list[str]:
    """Extract coding labels from a record's metadata.

    Each configured code field may hold a comma-separated string of
    labels (matching the corpus convention). Empty/missing fields
    contribute nothing.
    """
    labels: list[str] = []
    for field in code_fields:
        raw = record.metadata.get(field)
        if not isinstance(raw, str):
            continue
        labels.extend(c.strip() for c in raw.split(",") if c.strip())
    return labels


def build_coding_trend_table(
    records: tuple[FeedbackRecordModel, ...],
    *,
    date_field: str,
    code_fields: Sequence[str],
) -> CodingTrendTable | None:
    """Build a code-by-period count table from record metadata.

    Parameters
    ----------
    records : tuple[FeedbackRecordModel, ...]
        The full input record set.
    date_field : str
        Metadata key holding the record's date (parsed to a ``YYYY-MM``
        period).
    code_fields : Sequence[str]
        Metadata keys holding coding labels (comma-separated strings).

    Returns
    -------
    CodingTrendTable | None
        The assembled table, or ``None`` when no record carries a
        parseable date in ``date_field`` (best-effort omission).
    """
    counter: Counter[tuple[str, str]] = Counter()
    periods: set[str] = set()

    for record in records:
        period = _period_of(record.metadata.get(date_field))
        if period is None:
            continue
        periods.add(period)
        for code in _codes_in_record(record, code_fields):
            counter[(code, period)] += 1

    if not periods:
        return None

    cells = tuple(
        CodingTrendCell(code=code, period=period, count=count)
        for (code, period), count in sorted(counter.items())
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
