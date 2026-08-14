"""Rewrite feedback-record mentions in LLM output as EspoCRM hyperlinks.

This is *output* post-processing, not prompt assembly: the LLM answers in
prose that names records by their ``id``, and this turns those mentions
into links the analyst can click. It lives in its own module because more
than one use case needs it — the analyse result and the aggregate summary
both go back to EspoCRM — and neither of those services should have to
import the other to reach it.
"""

import re

from qfa.domain.models import FeedbackRecordModel


def hyperlink_form_references(
    text: str,
    feedback_records: tuple[FeedbackRecordModel, ...],
    espo_feedback_base_url: str | None,
) -> str:
    """Rewrite feedback-record-id mentions in ``text`` as EspoCRM hyperlinks.

    When the analysis/summary text names a feedback record by its ``id``
    (e.g. ``Form-07762``), rewrite that mention as
    ``[Form-07762](espo_feedback_base_url/url_id)`` so it renders as a
    clickable link back to the record in EspoCRM. No-op when
    ``espo_feedback_base_url`` is not provided; per-record no-op when that
    record has no ``url_id``. Matches on word boundaries so one record's id
    cannot match as a substring of another's (e.g. ``Form-1`` vs
    ``Form-10``).
    """
    if not espo_feedback_base_url:
        return text
    base = espo_feedback_base_url.rstrip("/")
    for record in feedback_records:
        if not record.url_id:
            continue
        link = f"[{record.id}]({base}/{record.url_id})"
        text = re.sub(rf"\b{re.escape(record.id)}\b", link, text)
    return text
