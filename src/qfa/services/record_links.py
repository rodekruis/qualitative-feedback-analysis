"""Render citation markers without imposing a layout on the model's report."""

import re
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qfa.domain.errors import AnalysisError
from qfa.domain.models import CommunityMeetingRecordModel, FeedbackRecordModel

MAX_INLINE_RECORD_IDS = 5
_MARKER = re.compile(r"\[\[qfa:([A-Za-z0-9_-]+)\]\]")
_OPEN = "<qfa-citations>"
_CLOSE = "</qfa-citations>"
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class _Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    title: str
    ids: list[str] = Field(min_length=1)
    count: str


class _CitationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str
    heading: str
    pointer: str
    citations: list[_Citation] = Field(min_length=1)


def hyperlink_form_references(
    text: str,
    feedback_records: tuple[FeedbackRecordModel | CommunityMeetingRecordModel, ...],
    espo_feedback_base_url: str | None,
    espo_meeting_base_url: str | None = None,
) -> str:
    """Link complete known ids, leaving missing URLs and existing links alone."""
    return _link_applier(
        feedback_records, espo_feedback_base_url, espo_meeting_base_url
    )(text)


def _link_applier(
    records: tuple[FeedbackRecordModel | CommunityMeetingRecordModel, ...],
    espo_feedback_base_url: str | None,
    espo_meeting_base_url: str | None,
) -> Callable[[str], str]:
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
        links[record.id] = f"[{record.id}]({base_url.rstrip('/')}/{record.url_id})"
    if not links:
        return lambda text: text
    references = "|".join(
        re.escape(reference) for reference in sorted(links, key=len, reverse=True)
    )
    pattern = re.compile(rf"(?<![\w\[-])({references})(?![\w])")
    return lambda text: pattern.sub(lambda match: links[match.group(1)], text)


def _plain(text: str) -> None:
    # Presidio placeholders are restored only after the JSON has been parsed.
    literal = re.sub(r"<[A-Z][A-Z_]*_\d+>", "", text)
    if not text.strip() or any(char in literal for char in "\r\n|<>[]*#`"):
        raise AnalysisError("Citation metadata must be nonempty single-line plain text")


def _template(text: str, values: dict[str, str]) -> str:
    _plain(text)
    fields = _PLACEHOLDER.findall(text)
    remainder = _PLACEHOLDER.sub("", text)
    if (
        sorted(fields) != sorted(values)
        or "{" in remainder
        or "}" in remainder
        or any(char.isdigit() for char in remainder)
    ):
        raise AnalysisError("Invalid citation template placeholders or numeric counts")
    return _PLACEHOLDER.sub(lambda match: values[match[1]], text)


def format_record_citations(
    text: str,
    records: tuple[FeedbackRecordModel | CommunityMeetingRecordModel, ...],
    espo_feedback_base_url: str | None,
    espo_meeting_base_url: str | None = None,
) -> str:
    """Render validated citation metadata; leave the surrounding Markdown intact.

    The existing generation call supplies translated wording and marker locations.
    Python deduplicates ids, calculates counts and moves lists above five ids to
    a closing section. Unknown ids remain visible, unlinked, and counted as such.
    Malformed contracts raise AnalysisError instead of leaking internal markers.
    Plain answers without citations need no metadata.
    """
    apply_links = _link_applier(records, espo_feedback_base_url, espo_meeting_base_url)
    if not any(
        token in text for token in ("<qfa-citations", "</qfa-citations", "[[qfa:")
    ):
        return apply_links(text)
    if text.count(_OPEN) != 1 or text.count(_CLOSE) != 1:
        raise AnalysisError("Expected one closing citation metadata block")
    body, _, rest = text.partition(_OPEN)
    payload, _, trailing = rest.partition(_CLOSE)
    if trailing.strip():
        raise AnalysisError("Citation metadata must be at the end of the answer")
    try:
        metadata = _CitationMetadata.model_validate_json(payload)
    except ValidationError as exc:
        raise AnalysisError("LLM returned invalid citation metadata") from exc

    _plain(metadata.label)
    _plain(metadata.heading)
    pointer = _template(metadata.pointer, {"heading": metadata.heading})
    markers = _MARKER.findall(body)
    keys = [citation.key for citation in metadata.citations]
    if len(set(keys)) != len(keys) or sorted(markers) != sorted(keys):
        raise AnalysisError("Citation markers and metadata must match one-to-one")
    if "[[qfa:" in _MARKER.sub("", body):
        raise AnalysisError("Malformed citation marker")

    kinds = {
        record.id: isinstance(record, CommunityMeetingRecordModel)
        for record in records
        if record.id
    }
    rendered: dict[str, str] = {}
    moved: dict[str, str] = {}
    for citation in metadata.citations:
        _plain(citation.title)
        ids = list(dict.fromkeys(citation.ids))
        if any(
            not item.strip() or any(char in item for char in "\r\n|<>[]*#`")
            for item in ids
        ):
            raise AnalysisError("Invalid cited record id")
        meetings = sum(kinds.get(item) is True for item in ids)
        feedback = sum(kinds.get(item) is False for item in ids)
        unmatched = len(ids) - feedback - meetings
        values = (
            {"count": str(len(ids))}
            if not unmatched and not (feedback and meetings)
            else {
                name: str(count)
                for name, count in (
                    ("feedback", feedback),
                    ("meetings", meetings),
                    ("unmatched", unmatched),
                )
                if count
            }
        )
        count = _template(citation.count, values)
        linked_ids = apply_links(", ".join(ids))
        if len(ids) <= MAX_INLINE_RECORD_IDS:
            rendered[citation.key] = f"**{metadata.label}:** {linked_ids}"
        else:
            rendered[citation.key] = f"**{metadata.label}:** {count}. *{pointer}*"
            moved[citation.key] = f"#### {citation.title} ({count})\n{linked_ids}"
    parts = _MARKER.split(body)
    result = "".join(
        rendered[part] if index % 2 else apply_links(part)
        for index, part in enumerate(parts)
    ).rstrip()
    if moved:
        blocks = "\n\n".join(moved[key] for key in markers if key in moved)
        result += f"\n\n---\n\n### {metadata.heading}\n\n{blocks}"
    return result
