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
) -> str:
    """Rewrite feedback and meeting-record mentions as EspoCRM hyperlinks.

    When the analysis/summary text names a feedback record by its ``id``
    (e.g. ``Form-07762``), rewrite that mention as
    ``[Form-07762](espo_feedback_base_url/url_id)`` so it renders as a
    clickable link back to the record in EspoCRM. Bare numeric suffixes of
    ``Form-*`` and ``Meeting-*`` IDs are also accepted because models often
    omit the human-readable prefix in lists. Community meeting records use
    the ``CCommunityMeetingData`` entity fragment when the supplied base URL
    contains the feedback entity fragment. No-op when
    ``espo_feedback_base_url`` is not provided; per-record no-op when that
    record has no ``url_id``. Matches are boundary-safe so one record's id
    cannot match as a substring of another's (e.g. ``Form-1`` vs
    ``Form-10``), and existing Markdown links are left unchanged.
    """
    if not espo_feedback_base_url:
        return text
    base = espo_feedback_base_url.rstrip("/")
    links: dict[str, str] = {}
    short_id_candidates: dict[str, set[str]] = {}
    for record in feedback_records:
        if not record.url_id:
            continue
        record_base = base
        if isinstance(record, CommunityMeetingRecordModel):
            record_base = record_base.replace(
                "#CFeedbackData/view", "#CCommunityMeetingData/view", 1
            )
        link = f"[{record.id}]({record_base}/{record.url_id})"
        links[record.id] = link

        if isinstance(record, FeedbackRecordModel) and record.id.startswith("Form-"):
            short_id = record.id.removeprefix("Form-")
            short_id_candidates.setdefault(short_id, set()).add(record.id)
        elif isinstance(record, CommunityMeetingRecordModel) and record.id.startswith(
            "Meeting-"
        ):
            short_id = record.id.removeprefix("Meeting-")
            short_id_candidates.setdefault(short_id, set()).add(record.id)

    for short_id, record_ids in short_id_candidates.items():
        if len(record_ids) == 1:
            links[short_id] = links[next(iter(record_ids))]

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
