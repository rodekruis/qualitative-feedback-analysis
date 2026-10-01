"""Tests for record-reference hyperlink rewriting."""

from qfa.domain.models import (
    CommunityMeetingRecordMetadataModel,
    CommunityMeetingRecordModel,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
)
from qfa.services.record_links import hyperlink_form_references


def test_links_full_and_bare_feedback_form_ids():
    records = tuple(
        FeedbackRecordModel(
            id=f"Form-{record_id}",
            content="feedback",
            metadata=FeedbackRecordMetadataModel(),
            url_id=f"feedback-{record_id}",
        )
        for record_id in ("10820", "10821")
    )

    result = hyperlink_form_references(
        "Form-10820 and 10821.", records, "https://example/#CFeedbackData/view"
    )

    assert "[Form-10820](https://example/#CFeedbackData/view/feedback-10820)" in result
    assert "[Form-10821](https://example/#CFeedbackData/view/feedback-10821)" in result


def test_uses_community_meeting_entity_url_for_meeting_ids():
    record = CommunityMeetingRecordModel(
        id="Meeting-00007",
        meetingNotes="meeting",
        metadata=CommunityMeetingRecordMetadataModel(),
        url_id="meeting-record-id",
    )

    result = hyperlink_form_references(
        "Meeting-00007", (record,), "https://example/#CFeedbackData/view"
    )

    assert result == (
        "[Meeting-00007](https://example/#CCommunityMeetingData/view/meeting-record-id)"
    )


def test_does_not_wrap_an_existing_markdown_link_again():
    record = FeedbackRecordModel(
        id="Form-10820",
        content="feedback",
        metadata=FeedbackRecordMetadataModel(),
        url_id="feedback-record-id",
    )
    text = "[Form-10820](https://example/#CFeedbackData/view/feedback-record-id)"

    result = hyperlink_form_references(
        text, (record,), "https://example/#CFeedbackData/view"
    )

    assert result == text
