"""Tests for the source-language detection helper (#294)."""

import pytest

from qfa.services.language import detect_source_language


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The water pump in the camp has been broken for three weeks.", "en"),
        (
            "Merci beaucoup pour votre aide, la distribution etait bien organisee.",
            "fr",
        ),
        ("", None),
        ("   ", None),
        ("Water is dirty", None),
        ("12345 !!!", None),
    ],
)
def test_detects_language_or_declines(text, expected):
    """Detectable prose yields its ISO 639-1 code; short or noisy text yields ``None``.

    Why: #294 — pinning the LLM's output language is only an improvement if
    the detected code is right. Unguarded, langdetect calls "Water is dirty"
    Afrikaans without raising, so the short-text cases guard the length gate
    rather than langdetect's own exception path ("12345 !!!").
    """
    assert detect_source_language(text) == expected


def test_detects_language_of_text_longer_than_the_sample_window():
    """Truncating to the 2000-character sample still detects correctly.

    Why: detection is synchronous work on the event loop, so only a prefix of
    the record is inspected — that shortcut must not cost accuracy.
    """
    long_text = "The distribution point ran out of blankets again. " * 200

    assert len(long_text) > 2000
    assert detect_source_language(long_text) == "en"


def test_detection_is_deterministic_across_calls():
    """Repeated detection of the same text returns the same code.

    Why: langdetect samples randomly unless its factory is seeded, which
    would make a pinned output language vary between identical requests.
    """
    text = "La pompe est cassee depuis trois semaines dans le camp."

    assert detect_source_language(text) == detect_source_language(text)
