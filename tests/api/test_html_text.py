"""Tests for reducing EspoCRM rich-text fields to plain text."""

from qfa.api.html_text import html_to_text


def test_plain_text_is_returned_unchanged():
    """No tag means no rewrite: notes typed by hand must survive byte-for-byte."""
    notes = "Discussed water points.\n\n  Two households  raised 1 < 2 concerns."
    assert html_to_text(notes) == notes


def test_tags_are_removed_and_entities_resolved():
    text = html_to_text("<p>Caf&eacute; &amp; well</p>")
    assert text == "Café & well"


def test_style_and_script_content_is_dropped():
    """Word exports a multi-kilobyte ``mso-`` stylesheet ahead of any prose."""
    markup = (
        "<html><head><style>p.MsoNormal {mso-style-parent:'';}</style></head>"
        "<body><p>Attendance was low.</p></body></html>"
    )
    assert html_to_text(markup) == "Attendance was low."


def test_block_boundaries_become_line_breaks():
    text = html_to_text("<p>First point</p><p>Second point</p><br>Third")
    assert text == "First point\n\nSecond point\n\nThird"


def test_table_cells_are_separated():
    """Without a separator, adjacent cells would merge into one word."""
    markup = "<table><tr><td>Harshin</td><td>male</td></tr></table>"
    assert html_to_text(markup) == "Harshin\tmale"


def test_markup_whitespace_is_collapsed():
    """Word wraps source lines mid-sentence, so data newlines are not breaks."""
    markup = "<div>\n   Water   point\n   repaired\n</div>"
    assert html_to_text(markup) == "Water point repaired"


def test_word_markup_shrinks_below_the_field_limit():
    """The 422 this guards against: inline CSS dwarfing the prose (issue #381)."""
    style = "font-family:Calibri;mso-ascii-theme-font:minor-latin;color:#1F497D"
    markup = "".join(
        f'<p><span style="{style}">Point {i}.</span></p>' for i in range(2000)
    )
    assert len(markup) > 100_000
    text = html_to_text(markup)
    assert len(text) < 100_000
    assert text.startswith("Point 0.")


def test_unclosed_tags_do_not_lose_text():
    """Word HTML is routinely malformed; the parser must stay lenient."""
    assert html_to_text("<p><b>Agreed<p>Next steps") == "Agreed\nNext steps"
