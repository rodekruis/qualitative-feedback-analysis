"""Rewrite record mentions in LLM output as EspoCRM hyperlinks.

This is *output* post-processing, not prompt assembly: the LLM answers in
prose that names records by their ``id``, and this turns those mentions
into links the analyst can click. It lives in its own module because more
than one use case needs it — the analyse result and the aggregate summary
both go back to EspoCRM — and neither of those services should have to
import the other to reach it.

:func:`format_record_citations` is the analyse entry point: it also folds
long ``Records:`` lines. Summaries keep calling
:func:`hyperlink_form_references`.
"""

import re
from collections.abc import Callable

from qfa.domain.models import CommunityMeetingRecordModel, FeedbackRecordModel

MAX_INLINE_RECORD_IDS = 5
"""A ``Records:`` line with more ids than this is folded."""

_CITATION_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?[^\W\d_][^:\uff1a\n]{0,39}?(?:\*\*|__)?"
    r"\s*[:\uff1a]\s*(?:\*\*|__)?\s*(?P<ids>\S.*?)\s*$"
)


def hyperlink_form_references(
    text: str,
    feedback_records: tuple[FeedbackRecordModel | CommunityMeetingRecordModel, ...],
    espo_feedback_base_url: str | None,
    espo_meeting_base_url: str | None = None,
) -> str:
    """Rewrite feedback and meeting-record mentions as EspoCRM hyperlinks.

    When the analysis/summary text names a feedback record by its ``id``
    (e.g. ``Form-07762``), rewrite that mention as
    ``[Form-07762](espo_feedback_base_url/url_id)`` so it renders as a
    clickable link back to the record in EspoCRM. Meeting records use
    ``espo_meeting_base_url``. Only complete record IDs are matched, so
    unrelated numbers in the analysis are never rewritten. No-op when the
    matching base URL is not provided; per-record
    no-op when that record has no ``url_id``. Matches are boundary-safe so one record's id
    cannot match as a substring of another's (e.g. ``Form-1`` vs
    ``Form-10``), and existing Markdown links are left unchanged.
    """
    apply_links = _link_applier(
        feedback_records, espo_feedback_base_url, espo_meeting_base_url
    )
    return apply_links(text)


def _link_applier(
    records: tuple[FeedbackRecordModel | CommunityMeetingRecordModel, ...],
    espo_feedback_base_url: str | None,
    espo_meeting_base_url: str | None,
) -> Callable[[str], str]:
    """Return a function that links known ids in a text; build the map once."""
    if not espo_feedback_base_url and not espo_meeting_base_url:
        return lambda text: text
    links: dict[str, str] = {}
    for record in records:
        base_url = (
            espo_meeting_base_url
            if isinstance(record, CommunityMeetingRecordModel)
            else espo_feedback_base_url
        )
        if not record.id or not record.url_id or not base_url:
            continue
        link = f"[{record.id}]({base_url.rstrip('/')}/{record.url_id})"
        links[record.id] = link

    if not links:
        return lambda text: text

    references = "|".join(
        re.escape(reference) for reference in sorted(links, key=len, reverse=True)
    )
    pattern = re.compile(rf"(?<![\w\[-])({references})(?![\w])")
    return lambda text: pattern.sub(lambda match: links[match.group(1)], text)


def format_record_citations(
    text: str,
    records: tuple[FeedbackRecordModel | CommunityMeetingRecordModel, ...],
    espo_feedback_base_url: str | None,
    espo_meeting_base_url: str | None = None,
) -> str:
    """Link record ids in an analysis and fold long ``Records:`` lines.

    A citation line is a short label, a colon, and a comma-separated list of
    single-token items, at least one of which is the ``id`` of a request
    record. The label word is not checked, so ``Registros:`` and
    ``**Records:**`` count too. A line with more than
    :data:`MAX_INLINE_RECORD_IDS` ids is folded (see :func:`_fold_markup`);
    shorter lines and ids in normal sentences are only linked. Unknown ids
    stay plain text, so an invented id stays visible.

    The fold summary is built from counts only, in English in every output
    language, never from record text or ids.
    """
    apply_links = _link_applier(records, espo_feedback_base_url, espo_meeting_base_url)
    kinds = {
        record.id: isinstance(record, CommunityMeetingRecordModel)
        for record in records
        if record.id
    }
    lines = []
    for line in text.split("\n"):
        folded = _fold_citation_line(line, kinds, apply_links)
        lines.append(apply_links(line) if folded is None else folded)
    return "\n".join(lines)


def _fold_citation_line(
    line: str,
    kinds: dict[str, bool],
    apply_links: Callable[[str], str],
) -> str | None:
    """Return the folded form of a long citation line, or ``None`` to keep it.

    ``kinds`` maps each request record id to ``True`` for a community meeting.
    """
    match = _CITATION_LINE.match(line)
    if match is None:
        return None
    items = list(dict.fromkeys(i.strip() for i in match["ids"].rstrip(".").split(",")))
    if not all(item and len(item.split()) == 1 for item in items):
        return None
    if not any(item in kinds for item in items):
        return None
    if len(items) <= MAX_INLINE_RECORD_IDS:
        return None

    meetings = sum(1 for item in items if kinds.get(item) is True)
    feedback = sum(1 for item in items if kinds.get(item) is False)
    unmatched = len(items) - meetings - feedback
    return _fold_markup(
        _count_label(feedback, meetings, unmatched), apply_links(", ".join(items))
    )


def _count_label(feedback: int, meetings: int, unmatched: int) -> str:
    """Count text for the fold summary, e.g. ``4 feedback records, 2 community meetings``."""
    if not unmatched and not (feedback and meetings):
        return f"{feedback + meetings} records"
    parts = [
        (feedback, "feedback record", "feedback records"),
        (meetings, "community meeting", "community meetings"),
        (unmatched, "unmatched id", "unmatched ids"),
    ]
    return ", ".join(
        f"{count} {one if count == 1 else many}" for count, one, many in parts if count
    )


def _fold_markup(summary: str, linked_ids: str) -> str:
    """Wrap ``linked_ids`` in a collapsible block.

    The one place that knows the fold shape, so a change of shape touches only
    this function.
    """
    return f"<details><summary>{summary}</summary>\n\n{linked_ids}\n\n</details>"
