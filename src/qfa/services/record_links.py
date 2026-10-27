"""Rewrite record mentions in LLM output as EspoCRM hyperlinks.

This is *output* post-processing, not prompt assembly: the LLM answers in
prose that names records by their ``id``, and this turns those mentions
into links the analyst can click. It lives in its own module because more
than one use case needs it — the analyse result and the aggregate summary
both go back to EspoCRM — and neither of those services should have to
import the other to reach it.

:func:`format_record_citations` is the analyse entry point: it also moves
long ``Records:`` lists to the end of the answer. Summaries keep calling
:func:`hyperlink_form_references`.
"""

import re
from collections.abc import Callable
from typing import NamedTuple

from qfa.domain.models import CommunityMeetingRecordModel, FeedbackRecordModel

MAX_INLINE_RECORD_IDS = 5
"""A ``Records:`` line with more ids than this moves to the end of the answer."""

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.*?)\s*#*\s*$")

OVERFLOW_HEADING = "Records per point"
"""Default title of the closing section that holds the long lists."""

_POINTER_TEXT = 'See "{heading}" at the end.'

_CITATION_LINE = re.compile(
    r"^(?P<lead>\s*(?:[-*+]\s+)?)(?:\*\*|__)?(?P<label>[^\W\d_][^:\uff1a\n]{0,39}?)"
    r"(?:\*\*|__)?\s*(?P<colon>[:\uff1a])\s*(?:\*\*|__)?\s*(?P<ids>\S.*?)\s*$"
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
    *,
    translate: Callable[[str], str] = lambda text: text,
) -> str:
    """Link record ids in an analysis and move long ``Records:`` lists to the end.

    A citation line is a short label, a colon, and a comma-separated list of
    single-token items, at least one of which is the ``id`` of a request
    record. The label word is not checked, so ``Registros:`` and
    ``**Records:**`` count too. A line with more than
    :data:`MAX_INLINE_RECORD_IDS` ids is replaced by a pointer line with the
    count, and its linked list goes under ``### Records per point`` at the end
    (see :func:`_pointer_line`). Shorter lines keep their ids inline. Every
    citation line gets a bold label, and ids in normal sentences are only
    linked. Unknown ids stay plain text, so an invented id stays visible.

    Plain Markdown only: EspoCRM escapes raw HTML in Text fields, so a
    ``<details>`` fold shows as literal text. ``translate`` localises only
    generated labels, counts and navigation, never record text or ids.
    """
    apply_links = _link_applier(records, espo_feedback_base_url, espo_meeting_base_url)
    kinds = {
        record.id: isinstance(record, CommunityMeetingRecordModel)
        for record in records
        if record.id
    }
    lines: list[str] = []
    moved: list[tuple[str, str, str]] = []
    heading = ""
    for line in text.split("\n"):
        if heading_match := _HEADING.match(line):
            heading = heading_match["title"]
        citation = _parse_citation_line(line, kinds)
        if citation is None:
            lines.append(apply_links(line))
        elif len(citation.ids) <= MAX_INLINE_RECORD_IDS:
            lines.append(
                f"{citation.lead}**{translate(citation.label)}{citation.colon}** "
                f"{apply_links(citation.rest)}"
            )
        else:
            count = translate(_count_label(*_count_by_kind(citation.ids, kinds)))
            lines.append(_pointer_line(count, translate))
            moved.append(
                (
                    heading or translate("Records"),
                    count,
                    apply_links(", ".join(citation.ids)),
                )
            )
    if moved:
        lines.append(_overflow_section(moved, translate(OVERFLOW_HEADING)))
    return "\n".join(lines)


class _CitationLine(NamedTuple):
    lead: str
    label: str
    colon: str
    rest: str
    ids: list[str]


def _parse_citation_line(line: str, kinds: dict[str, bool]) -> _CitationLine | None:
    """Parse a citation line, or return ``None`` if ``line`` is not one.

    ``kinds`` maps each request record id to ``True`` for a community meeting.
    ``ids`` is deduplicated, in order of first appearance.
    """
    match = _CITATION_LINE.match(line)
    if match is None:
        return None
    ids = list(dict.fromkeys(i.strip() for i in match["ids"].rstrip(".").split(",")))
    if not all(item and len(item.split()) == 1 for item in ids):
        return None
    if not any(item in kinds for item in ids):
        return None
    return _CitationLine(
        match["lead"], match["label"].strip("*_ "), match["colon"], match["ids"], ids
    )


def _count_by_kind(ids: list[str], kinds: dict[str, bool]) -> tuple[int, int, int]:
    """Count ``ids`` as ``(feedback, meetings, unmatched)``."""
    meetings = sum(1 for item in ids if kinds.get(item) is True)
    feedback = sum(1 for item in ids if kinds.get(item) is False)
    return feedback, meetings, len(ids) - meetings - feedback


def _count_label(feedback: int, meetings: int, unmatched: int) -> str:
    """Count text, e.g. ``4 feedback records, 2 community meetings``."""
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


def _pointer_line(label: str, translate: Callable[[str], str]) -> str:
    """Line that replaces a long ``Records:`` list inside its point.

    With :func:`_overflow_section`, the only place that knows the shape of a
    moved list, so a change of shape touches only these two functions.
    """
    pointer = translate(_POINTER_TEXT).replace("{heading}", translate(OVERFLOW_HEADING))
    return f"**{translate('Records')}:** {label}. *{pointer}*"


def _overflow_section(moved: list[tuple[str, str, str]], title: str) -> str:
    """Closing section with one ``####`` block per moved list.

    A ``---`` rule sets it apart: Markdown collapses extra blank lines.

    Each entry is ``(point heading, count label, linked ids)``.
    """
    blocks = "".join(
        f"\n#### {heading} ({label})\n{ids}\n" for heading, label, ids in moved
    )
    return f"\n---\n\n### {title}\n{blocks}".rstrip("\n")
