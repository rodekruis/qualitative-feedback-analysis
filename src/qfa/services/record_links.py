"""Rewrite record mentions in LLM output as EspoCRM hyperlinks.

This is *output* post-processing, not prompt assembly: the LLM answers in
prose that names records by their ``id``, and this turns those mentions
into links the analyst can click. It lives in its own module because more
than one use case needs it — the analyse result and the aggregate summary
both go back to EspoCRM — and neither of those services should have to
import the other to reach it.
"""

import re

from qfa.domain.models import CommunityMeetingRecordModel, FeedbackRecordModel


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
    if not espo_feedback_base_url and not espo_meeting_base_url:
        return text
    links: dict[str, str] = {}
    for record in feedback_records:
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
        return text

    references = "|".join(
        re.escape(reference) for reference in sorted(links, key=len, reverse=True)
    )
    return re.sub(
        rf"(?<![\w\[-])({references})(?![\w])",
        lambda match: links[match.group(1)],
        text,
    )
