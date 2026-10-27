"""Tests for layout-independent, localized citation rendering and hyperlinks."""

import json

import pytest

from qfa.domain.errors import AnalysisError
from qfa.domain.models import (
    CommunityMeetingRecordModel,
    FeedbackRecordModel,
)
from qfa.services.record_links import format_record_citations, hyperlink_form_references

FEEDBACK_URL = "https://example/#CFeedbackData/view"
MEETING_URL = "https://example/#CCommunityMeetingData/view"


def _feedback(n):
    return FeedbackRecordModel(id=f"Form-{n}", content="feedback", url_id=f"fb-{n}")


def _meeting(n):
    return CommunityMeetingRecordModel(
        id=f"Meeting-{n}", meetingNotes="meeting", url_id=f"mt-{n}"
    )


def _citation(ids, key="c1", title="Water", count="{count} records"):
    return {"key": key, "title": title, "ids": ids, "count": count}


def _citation_answer(body, citations, *, spanish=False):
    metadata = {
        "label": "Registros" if spanish else "Records",
        "heading": "Registros por punto" if spanish else "Records per point",
        "pointer": 'Consulte "{heading}" al final.'
        if spanish
        else 'See "{heading}" at the end.',
        "citations": citations,
    }
    return f"{body}\n<qfa-citations>{json.dumps(metadata)}</qfa-citations>"


def test_links_full_feedback_ids_without_rewriting_matching_bare_numbers():
    records = tuple(_feedback(n) for n in (10820, 120, 2026))
    result = hyperlink_form_references(
        "Form-10820, 120 people said it was late, and in 2026 the queue grew.",
        records,
        FEEDBACK_URL,
    )
    assert result == (
        f"[Form-10820]({FEEDBACK_URL}/fb-10820), "
        "120 people said it was late, and in 2026 the queue grew."
    )


def test_uses_community_meeting_entity_url_for_meeting_ids():
    assert hyperlink_form_references(
        "Meeting-7", (_meeting(7),), FEEDBACK_URL, MEETING_URL
    ) == (f"[Meeting-7]({MEETING_URL}/mt-7)")


def test_meeting_without_its_base_url_is_not_linked_to_feedback_entity():
    assert (
        hyperlink_form_references("Meeting-7", (_meeting(7),), FEEDBACK_URL)
        == "Meeting-7"
    )


def test_does_not_wrap_an_existing_markdown_link_again():
    text = f"[Form-1]({FEEDBACK_URL}/fb-1)"
    assert hyperlink_form_references(text, (_feedback(1),), FEEDBACK_URL) == text


def test_link_matching_is_boundary_safe_and_skips_missing_url_ids():
    records = (_feedback(1), _feedback(10).model_copy(update={"url_id": ""}))
    assert hyperlink_form_references(
        "Form-1, Form-10, Form-100", records, FEEDBACK_URL
    ) == (f"[Form-1]({FEEDBACK_URL}/fb-1), Form-10, Form-100")


@pytest.mark.parametrize("size", [1, 5, 6, 37])
@pytest.mark.parametrize("spanish", [False, True])
@pytest.mark.parametrize("layout", ["default", "table", "list"])
def test_counts_threshold_and_language_without_rewriting_layout(size, spanish, layout):
    records = tuple(_feedback(n) for n in range(size))
    body = {
        "default": "### Agua\nLargas colas.\n[[qfa:c1]]",
        "table": "| Tema | Fuentes |\n| --- | --- |\n| Agua | [[qfa:c1]] |",
        "list": "- Agua: largas colas. [[qfa:c1]]",
    }[layout]
    text = _citation_answer(
        body,
        [
            _citation(
                [r.id for r in records],
                title="Agua",
                count="{count} registros" if spanish else "{count} records",
            ),
        ],
        spanish=spanish,
    )
    result = format_record_citations(text, records, FEEDBACK_URL)
    label = "Registros" if spanish else "Records"
    heading = "Registros por punto" if spanish else "Records per point"
    assert result.startswith(body.split("[[qfa:c1]]")[0])
    assert "<qfa-citations>" not in result
    assert "[[qfa:" not in result
    assert "**" + label + ":** " in result
    if size <= 5:
        assert "---\n\n###" not in result
        assert f"**{label}:** [Form-0]" in result
    else:
        count = f"{size} registros" if spanish else f"{size} records"
        pointer = (
            f'Consulte "{heading}" al final.'
            if spanish
            else f'See "{heading}" at the end.'
        )
        assert f"**{label}:** {count}. *{pointer}*" in result
        assert f"---\n\n### {heading}\n\n#### Agua ({count})" in result
    if layout == "table":
        assert len(result.splitlines()[2].split("|")) == 4
        assert result.splitlines()[2].endswith(" |")
    if spanish:
        assert "Records" not in result
    for record in records:
        assert result.count(f"[{record.id}]({FEEDBACK_URL}/{record.url_id})") == 1


def test_mixed_types_unknown_ids_and_translated_count_order():
    records = (*(_feedback(n) for n in range(4)), _meeting(1), _meeting(2))
    text = _citation_answer(
        "[[qfa:c1]]",
        [
            _citation(
                [r.id for r in records] + ["Unknown-1"],
                title="Agua",
                count="{meetings} reuniones, {feedback} comentarios, {unmatched} id desconocido",
            ),
        ],
        spanish=True,
    )
    result = format_record_citations(text, records, FEEDBACK_URL, MEETING_URL)
    assert "(2 reuniones, 4 comentarios, 1 id desconocido)" in result
    assert f"[Meeting-1]({MEETING_URL}/mt-1)" in result
    assert f"[Form-1]({FEEDBACK_URL}/fb-1)" in result
    assert result.endswith(", Unknown-1")
    assert "[Unknown-1]" not in result


def test_duplicate_ids_are_counted_and_listed_once():
    records = tuple(_feedback(n) for n in range(6))
    text = _citation_answer("[[qfa:c1]]", [_citation([r.id for r in records] * 2)])
    result = format_record_citations(text, records, None)
    assert "6 records" in result
    assert "12 records" not in result
    for record in records:
        assert result.count(record.id) == 1


def test_meeting_only_counts_and_no_base_urls():
    records = tuple(_meeting(n) for n in range(6))
    result = format_record_citations(
        _citation_answer("[[qfa:c1]]", [_citation([r.id for r in records])]),
        records,
        None,
    )
    assert "6 records" in result
    assert "Meeting-0, Meeting-1" in result
    assert "](" not in result


def test_appendix_order_follows_report_not_metadata_order():
    records = tuple(_feedback(n) for n in range(12))
    text = _citation_answer(
        "### A\n[[qfa:first]]\n\n### B\n[[qfa:second]]",
        [
            _citation([r.id for r in records[6:]], "second", "B"),
            _citation([r.id for r in records[:6]], "first", "A"),
        ],
    )
    result = format_record_citations(text, records, FEEDBACK_URL)
    assert result.count("### Records per point") == 1
    assert result.index("#### A") < result.index("#### B")


def test_plain_answer_without_citation_contract_and_prose_references():
    records = (_feedback(1),)
    assert (
        format_record_citations("No supported findings.", records, None)
        == "No supported findings."
    )
    assert format_record_citations("See Form-1.", records, FEEDBACK_URL) == (
        f"See [Form-1]({FEEDBACK_URL}/fb-1)."
    )


def test_linking_prose_cannot_rewrite_marker_keys_that_match_record_ids():
    record = _feedback(1).model_copy(update={"url_id": "Form-1"})
    text = _citation_answer(
        "See Form-1.\n[[qfa:Form-1]]",
        [_citation(["Form-1"], key="Form-1")],
    )
    link = f"[Form-1]({FEEDBACK_URL}/Form-1)"
    assert format_record_citations(text, (record,), FEEDBACK_URL) == (
        f"See {link}.\n**Records:** {link}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "[[qfa:c1]]",
        "<qfa-citations>{}</qfa-citations>",
        "<qfa-citations>not JSON</qfa-citations>",
        "<qfa-citations>{}",
        "</qfa-citations>",
        "<qfa-citations >{}</qfa-citations >",
        _citation_answer("[[qfa:c1]]", [_citation(["Form-1"])]) + "trailing prose",
        _citation_answer("[[qfa:c1]]", [_citation(["Form-1"])]) * 2,
        _citation_answer("[[qfa:bad key]]", [_citation(["Form-1"])]),
        _citation_answer("[[qfa:missing]]", [_citation(["Form-1"])]),
        _citation_answer("No marker", [_citation(["Form-1"])]),
        _citation_answer("[[qfa:c1]] [[qfa:c1]]", [_citation(["Form-1"])]),
        _citation_answer("[[qfa:c1]]", [_citation(["Form-1"]), _citation(["Form-2"])]),
        _citation_answer("[[qfa:c1]]", [_citation([])]),
        _citation_answer("[[qfa:c1]]", [_citation([""])]),
        _citation_answer("[[qfa:c1]]", [_citation([123])]),
        _citation_answer("[[qfa:c1]]", [_citation(["Form-1"], title="A\n### Extra")]),
        _citation_answer("[[qfa:c1]]", [_citation(["Form-1"], title="A | B")]),
        _citation_answer("[[qfa:c1]]", [_citation(["Form-1"], count="37 records")]),
        _citation_answer(
            "[[qfa:c1]]", [_citation(["Form-1"], count="{count} {count} records")]
        ),
        _citation_answer(
            "[[qfa:c1]]", [_citation(["Form-1"], count="{count!r} records")]
        ),
        _citation_answer(
            "[[qfa:c1]]", [_citation(["Form-1"], count="{count.__class__}")]
        ),
        _citation_answer(
            "[[qfa:c1]]", [_citation(["Form-1"], count="{feedback} records")]
        ),
        _citation_answer(
            "[[qfa:c1]]", [_citation(["Form-1"], count="**{count} records**")]
        ),
    ],
)
def test_invalid_contracts_fail_explicitly(text):
    with pytest.raises(AnalysisError):
        format_record_citations(text, (_feedback(1),), FEEDBACK_URL)


@pytest.mark.parametrize(
    "field,value",
    [
        ("label", ""),
        ("heading", "A | B"),
        ("pointer", "See the end."),
        ("pointer", "See {heading} and {heading}"),
        ("pointer", "See {other}"),
        ("extra", "unexpected"),
    ],
)
def test_invalid_labels_and_pointer_templates_fail(field, value):
    text = _citation_answer("[[qfa:c1]]", [_citation(["Form-1"])])
    body, _, rest = text.partition("<qfa-citations>")
    metadata = json.loads(rest.removesuffix("</qfa-citations>"))
    metadata[field] = value
    with pytest.raises(AnalysisError):
        format_record_citations(
            body + "<qfa-citations>" + json.dumps(metadata) + "</qfa-citations>",
            (_feedback(1),),
            FEEDBACK_URL,
        )
