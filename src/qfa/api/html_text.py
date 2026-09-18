"""Plain-text extraction for rich-text fields.

EspoCRM's WYSIWYG editor stores whatever the user pastes. Notes pasted from
Word arrive as tens of thousands of characters of inline CSS, ``mso-`` style
blocks and base64 spell-check images wrapping a few kilobytes of prose — enough
to blow the field's length cap and, under it, to bury the discussion in markup
the model still pays tokens for.

Applied to community meeting notes only. Feedback ``content`` reaches the API
from forms and SMS, never from a rich-text editor, so it is left alone.
"""

import re
from html.parser import HTMLParser

# Tags whose *content* is markup or document metadata, never prose.
_DROPPED = frozenset({"head", "script", "style", "title"})

# Tags that end a visual line.
_BREAKS = frozenset(
    {
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "table",
        "tr",
    }
)

_CELLS = frozenset({"td", "th"})

_LOOKS_LIKE_HTML = re.compile(r"<[a-zA-Z/!]")
_ANY_WS = re.compile(r"\s+")
_PADDED_TAB = re.compile(r"[ \t]*\t[ \t]*")
_BLANK_RUN = re.compile(r"\n{3,}")


class _Extractor(HTMLParser):
    """Collects character data, turning block boundaries into whitespace."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._dropped_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED:
            self._dropped_depth += 1
        elif tag in _BREAKS:
            self.chunks.append("\n")
        elif tag in _CELLS:
            self.chunks.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._dropped_depth = max(self._dropped_depth - 1, 0)
        elif tag in _BREAKS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        # Word wraps source lines mid-sentence, so data newlines are not breaks.
        if self._dropped_depth == 0:
            self.chunks.append(_ANY_WS.sub(" ", data))


def html_to_text(value: str) -> str:
    r"""Return the prose inside ``value``, or ``value`` unchanged if it has no tags.

    Character references are resolved, block boundaries become newlines and
    table cells are separated by tabs so adjacent cells don't run together. All
    other whitespace collapses, leaving at most one blank line. Values with no
    tag are returned byte-for-byte, so plain-text notes are never rewritten.
    """
    if not _LOOKS_LIKE_HTML.search(value):
        return value

    parser = _Extractor()
    parser.feed(value)
    parser.close()

    text = _PADDED_TAB.sub("\t", "".join(parser.chunks))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()
