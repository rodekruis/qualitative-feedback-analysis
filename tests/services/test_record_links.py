"""Tests for record-reference hyperlink rewriting."""

from qfa.domain.models import (
    CommunityMeetingRecordMetadataModel,
    CommunityMeetingRecordModel,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
)
from qfa.services.record_links import (
    format_record_citations,
    hyperlink_form_references,
)


def test_links_full_feedback_ids_without_rewriting_matching_bare_numbers():
    records = tuple(
        FeedbackRecordModel(
            id=f"Form-{record_id}",
            content="feedback",
            metadata=FeedbackRecordMetadataModel(),
            url_id=f"feedback-{record_id}",
        )
        for record_id in ("10820", "120", "2026")
    )

    result = hyperlink_form_references(
        "Form-10820, 120 people said it was late, and in 2026 the queue grew.",
        records,
        "https://example/#CFeedbackData/view",
    )

    assert result == (
        "[Form-10820](https://example/#CFeedbackData/view/feedback-10820), "
        "120 people said it was late, and in 2026 the queue grew."
    )


def test_uses_community_meeting_entity_url_for_meeting_ids():
    record = CommunityMeetingRecordModel(
        id="Meeting-00007",
        meetingNotes="meeting",
        metadata=CommunityMeetingRecordMetadataModel(),
        url_id="meeting-record-id",
    )

    result = hyperlink_form_references(
        "Meeting-00007",
        (record,),
        "https://example/#CFeedbackData/view",
        "https://example/#CCommunityMeetingData/view",
    )

    assert result == (
        "[Meeting-00007](https://example/#CCommunityMeetingData/view/meeting-record-id)"
    )


def test_meeting_without_its_base_url_is_not_linked_to_feedback_entity():
    record = CommunityMeetingRecordModel(
        id="Meeting-00007",
        meetingNotes="meeting",
        metadata=CommunityMeetingRecordMetadataModel(),
        url_id="meeting-record-id",
    )

    result = hyperlink_form_references(
        "Meeting-00007", (record,), "https://example/#CFeedbackData/view"
    )

    assert result == "Meeting-00007"


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


FEEDBACK_URL = "https://example/#CFeedbackData/view"
MEETING_URL = "https://example/#CCommunityMeetingData/view"


def _feedback(n: int) -> FeedbackRecordModel:
    return FeedbackRecordModel(
        id=f"Form-{n:05d}",
        content="feedback",
        metadata=FeedbackRecordMetadataModel(),
        url_id=f"fb-{n}",
    )


def _meeting(n: int) -> CommunityMeetingRecordModel:
    return CommunityMeetingRecordModel(
        id=f"MTG-{n:04d}",
        meetingNotes="meeting",
        metadata=CommunityMeetingRecordMetadataModel(),
        url_id=f"mt-{n}",
    )


def _ids(records) -> str:
    return ", ".join(record.id for record in records)


SECTION = "### Records per point"


class TestFormatRecordCitations:
    def test_short_line_stays_inline_with_links(self):
        records = tuple(_feedback(n) for n in range(1, 6))
        text = f"### Water\nLong queues.\nRecords: {_ids(records)}"

        result = format_record_citations(text, records, FEEDBACK_URL)

        assert SECTION not in result
        assert result.startswith("### Water\nLong queues.\nRecords: [Form-00001](")
        assert f"[Form-00005]({FEEDBACK_URL}/fb-5)" in result

    def test_long_line_moves_to_the_end_and_leaves_a_pointer(self):
        records = tuple(_feedback(n) for n in range(1, 7))
        text = f"### Water\nLong queues.\nRecords: {_ids(records)}"

        result = format_record_citations(text, records, FEEDBACK_URL)

        head, _, tail = result.partition(f"\n{SECTION}\n")
        assert head == (
            '### Water\nLong queues.\nRecords: 6 records. See "Records per point" '
            "at the end.\n"
        )
        assert tail.startswith("\n#### Water (6 records)\n[Form-00001](")
        assert f"[Form-00006]({FEEDBACK_URL}/fb-6)" in tail

    def test_no_html_is_written(self):
        records = tuple(_feedback(n) for n in range(1, 8))

        result = format_record_citations(
            f"### A\nText.\nRecords: {_ids(records)}", records, FEEDBACK_URL
        )

        assert "<" not in result

    def test_each_id_appears_once(self):
        records = tuple(_feedback(n) for n in range(1, 8))

        result = format_record_citations(
            f"### A\nText.\nRecords: {_ids(records)}", records, FEEDBACK_URL
        )

        for record in records:
            assert result.count(f"[{record.id}](") == 1

    def test_mixed_types_get_a_label_per_type_and_their_own_base_url(self):
        records = (*(_feedback(n) for n in range(1, 5)), _meeting(1), _meeting(2))
        text = f"### Water\nRecords: {_ids(records)}"

        result = format_record_citations(text, records, FEEDBACK_URL, MEETING_URL)

        assert "#### Water (4 feedback records, 2 community meetings)" in result
        assert f"[Form-00001]({FEEDBACK_URL}/fb-1)" in result
        assert f"[MTG-0001]({MEETING_URL}/mt-1)" in result

    def test_unknown_id_stays_plain_text_in_the_moved_list(self):
        records = tuple(_feedback(n) for n in range(1, 7))
        text = f"### A\nRecords: {_ids(records)}, Form-99999"

        result = format_record_citations(text, records, FEEDBACK_URL)

        assert "(6 feedback records, 1 unmatched id)" in result
        assert ", Form-99999" in result
        assert "[Form-99999]" not in result

    def test_counts_never_contain_ids(self):
        records = tuple(_feedback(n) for n in range(1, 8))

        result = format_record_citations(
            f"### A\nRecords: {_ids(records)}", records, FEEDBACK_URL
        )

        pointer = next(line for line in result.split("\n") if "See " in line)
        assert pointer == 'Records: 7 records. See "Records per point" at the end.'

    def test_translated_or_decorated_labels_are_still_found(self):
        records = tuple(_feedback(n) for n in range(1, 7))
        for line in (
            f"Registros: {_ids(records)}",
            f"**Records:** {_ids(records)}",
            f"**Records**: {_ids(records)}",
            f"- Records: {_ids(records)}",
        ):
            result = format_record_citations(line, records, FEEDBACK_URL)

            assert SECTION in result, line

    def test_without_base_url_the_list_still_moves_and_ids_stay_plain(self):
        records = tuple(_feedback(n) for n in range(1, 7))

        result = format_record_citations(
            f"### A\nRecords: {_ids(records)}", records, None
        )

        assert SECTION in result
        assert "Form-00001, Form-00002" in result
        assert "](" not in result

    def test_a_long_list_without_a_heading_uses_a_generic_title(self):
        records = tuple(_feedback(n) for n in range(1, 7))

        result = format_record_citations(f"Records: {_ids(records)}", records, None)

        assert "#### Records (6 records)" in result

    def test_ids_in_a_normal_sentence_are_linked_but_never_moved(self):
        records = tuple(_feedback(n) for n in range(1, 8))
        text = f"Queues were long ({_ids(records)}) in the morning."

        result = format_record_citations(text, records, FEEDBACK_URL)

        assert SECTION not in result
        assert f"[Form-00001]({FEEDBACK_URL}/fb-1)" in result

    def test_a_colon_line_without_ids_is_left_alone(self):
        records = tuple(_feedback(n) for n in range(1, 3))
        text = "Note: water was scarce, and people waited."

        assert format_record_citations(text, records, FEEDBACK_URL) == text

    def test_a_line_that_mixes_prose_and_ids_is_not_a_citation_line(self):
        records = tuple(_feedback(n) for n in range(1, 8))
        text = f"Examples: {_ids(records)} and many others waited."

        result = format_record_citations(text, records, FEEDBACK_URL)

        assert SECTION not in result

    def test_each_long_point_gets_its_own_block_under_one_section(self):
        records = tuple(_feedback(n) for n in range(1, 13))
        first, second = records[:6], records[6:]
        text = (
            f"### A\nText.\nRecords: {_ids(first)}\n\n"
            f"### B\nText.\nRecords: {_ids(second)}"
        )

        result = format_record_citations(text, records, FEEDBACK_URL)

        assert result.count(SECTION) == 1
        assert result.index("#### A (6 records)") < result.index("#### B (6 records)")

    def test_repeated_ids_count_once(self):
        records = tuple(_feedback(n) for n in range(1, 4))
        text = f"Records: {_ids(records)}, {_ids(records)}"

        result = format_record_citations(text, records, FEEDBACK_URL)

        assert SECTION not in result
