"""Tests for the Presidio-based anonymisation adapter."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from qfa.adapters.presidio_anonymizer import (
    LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS,
    PresidioAnonymizer,
)


@pytest.fixture(scope="module")
def anonymizer() -> PresidioAnonymizer:
    return PresidioAnonymizer()


@pytest.mark.parametrize(
    "input_text, output_must_contain, sensitive_bit",
    [
        ("Hi my name is Dick Schoof", "<PERSON", "Dick Schoof"),
        ("My number is 212-555-5555", "<PHONE_NUMBER", "212-555-5555"),
        ("I live in The Netherlands", "<LOCATION", "The Netherlands"),
        ("Je m'appelle Marie Dupont", "<PERSON", "Marie Dupont"),  # French
        ("Me llamo Carlos García", "<PERSON", "Carlos García"),  # Spanish
        ("Мене звати Олена Петренко", "<PERSON", "Олена Петренко"),  # Ukrainian
        ("Меня зовут Иван Иванов", "<PERSON", "Иван Иванов"),  # Russian
    ],
)
def test_anonymize_replaces_entity_and_roundtrip_restores_original(
    anonymizer: PresidioAnonymizer,
    input_text: str,
    output_must_contain: str,
    sensitive_bit: str,
) -> None:
    anonymized_text, mapping = anonymizer.anonymize(input_text)
    assert output_must_contain in anonymized_text
    assert sensitive_bit not in anonymized_text

    deanonymized_text = anonymizer.deanonymize(anonymized_text, mapping)
    assert sensitive_bit in deanonymized_text


def test_date_entities_are_preserved_verbatim(
    anonymizer: PresidioAnonymizer,
) -> None:
    anonymized_text, mapping = anonymizer.anonymize(
        "I have a meeting on September 1st."
    )
    assert "September 1st" in anonymized_text
    assert len(mapping) == 0


def test_anonymize_handles_undetectable_language_gracefully(
    anonymizer: PresidioAnonymizer,
) -> None:
    # This string is just random characters unlikely to be detected as any language
    input_text = "asdkjhasdkjh asdkljhasd asdkljhasd"
    anonymized_text, mapping = anonymizer.anonymize(input_text)
    assert anonymized_text == input_text  # No anonymization should occur
    assert len(mapping) == 0  # No mappings should be created


def test_anonymize_handles_empty_string(
    anonymizer: PresidioAnonymizer,
) -> None:
    input_text = ""
    anonymized_text, mapping = anonymizer.anonymize(input_text)
    assert anonymized_text == input_text  # No change to empty string
    assert len(mapping) == 0  # No mappings should be created


@pytest.mark.parametrize(
    "input_text, expected_language",
    [
        ("This is an English sentence.", "en"),
        ("C'est une phrase française.", "fr"),
        ("Esta es una oración en español.", "es"),
        ("Це речення українською.", "uk"),
        ("Это предложение на русском.", "ru"),
        ("asdkjhasdkjh asdkljhasd asdkljhasd", "xx"),  # Undetectable language
        ("", "xx"),  # Empty string
    ],
)
def test_detect_language_returns_expected_language_code(
    input_text: str,
    expected_language: str,
) -> None:
    from qfa.adapters.presidio_anonymizer import detect_language

    detected_language = detect_language(input_text)
    assert detected_language == expected_language


# Presidio applies operators in reverse document order, so within one text the
# *later* entity gets the *lower* index. Assert on uniqueness and round-trip,
# never on which index a given entity received.


def test_batch_gives_distinct_records_distinct_placeholders(
    anonymizer: PresidioAnonymizer,
) -> None:
    """Regression test for #324: per-call numbering collided across records."""
    texts = (
        "Olena Kovalenko reported the water point in Kharkiv ran dry.",
        "Piet Jansen reported the water point in Utrecht ran dry.",
    )

    redacted, mapping = anonymizer.anonymize_batch(texts)

    assert len(set(mapping.values())) == len(mapping)
    assert {"Kharkiv", "Utrecht"} <= set(mapping.values())
    for original, anonymized_text in zip(texts, redacted, strict=True):
        assert anonymizer.deanonymize(anonymized_text, mapping) == original


def test_repeated_value_across_texts_reuses_one_placeholder(
    anonymizer: PresidioAnonymizer,
) -> None:
    redacted, mapping = anonymizer.anonymize_batch(
        (
            "Olena Kovalenko queued for water in Kharkiv.",
            "In Lviv we met Olena Kovalenko again.",
        )
    )

    placeholders = [p for p, value in mapping.items() if value == "Olena Kovalenko"]
    assert len(placeholders) == 1
    assert placeholders[0] in redacted[0]
    assert placeholders[0] in redacted[1]


def test_deanonymize_is_unambiguous_with_double_digit_indices(
    anonymizer: PresidioAnonymizer,
) -> None:
    """Double-digit indices must not be corrupted by prefix collisions.

    ``deanonymize`` is a plain substring replace, which is only safe
    because the trailing ``>`` stops ``<PERSON_1>`` matching inside
    ``<PERSON_10>``. This pins that invariant.
    """
    cities = (
        "Kharkiv",
        "Utrecht",
        "Lviv",
        "Odesa",
        "Amsterdam",
        "Rotterdam",
        "Nairobi",
        "Kampala",
        "Bogota",
        "Caracas",
        "Manila",
        "Jakarta",
        "Dakar",
        "Kinshasa",
    )
    texts = tuple(f"The water point in {city} ran dry." for city in cities)

    redacted, mapping = anonymizer.anonymize_batch(texts)

    assert any(int(p.rsplit("_", 1)[1].rstrip(">")) > 9 for p in mapping)
    for original, anonymized_text in zip(texts, redacted, strict=True):
        assert anonymizer.deanonymize(anonymized_text, mapping) == original


def test_anonymize_matches_a_single_text_batch(
    anonymizer: PresidioAnonymizer,
) -> None:
    text = "Hi my name is Dick Schoof and I live in The Netherlands"

    single_text, single_mapping = anonymizer.anonymize(text)
    (batched_text,), batched_mapping = anonymizer.anonymize_batch((text,))

    assert single_text == batched_text
    assert single_mapping == batched_mapping


def test_anonymize_uses_the_inline_fast_path_not_the_pool(
    anonymizer: PresidioAnonymizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-text call must never touch the thread pool.

    ``anonymize``/a one-chunk ``anonymize_batch`` runs synchronously inside
    ``async def`` on the event loop thread today (coding/summarize/
    sensitivity); submitting to the pool from there would add a needless
    thread hop for zero parallelism benefit.
    """
    mock_submit = MagicMock(wraps=anonymizer._pool.submit)
    monkeypatch.setattr(anonymizer._pool, "submit", mock_submit)

    anonymizer.anonymize("Hi my name is Dick Schoof")

    mock_submit.assert_not_called()


_MULTI_LANGUAGE_BATCH = (
    "Olena Kovalenko reported the water point in Kharkiv ran dry.",
    "Piet Jansen reported the water point in Utrecht ran dry.",
    "Je m'appelle Marie Dupont et j'habite à Lyon.",
    "Me llamo Carlos García y vivo en Madrid.",
    "Мене звати Олена Петренко.",
    "Меня зовут Иван Иванов.",
)


def test_worker_count_parity(anonymizer: PresidioAnonymizer) -> None:
    """Output must be identical whether detection runs serially or pooled.

    Swaps in a 1-worker pool rather than building a second real adapter
    (~3s, ~1.2GB to load six spaCy models) — restored after, since the
    module-scoped fixture is shared with every other test in this file.
    Never assert on which index a given entity received: Presidio applies
    operators in reverse document order, so that is not a stable property.
    """
    pooled_redacted, pooled_mapping = anonymizer.anonymize_batch(_MULTI_LANGUAGE_BATCH)

    original_pool = anonymizer._pool
    original_max_workers = anonymizer._max_workers
    anonymizer._pool = ThreadPoolExecutor(max_workers=1)
    anonymizer._max_workers = 1
    try:
        serial_redacted, serial_mapping = anonymizer.anonymize_batch(
            _MULTI_LANGUAGE_BATCH
        )
    finally:
        anonymizer._pool.shutdown(wait=True)
        anonymizer._pool = original_pool
        anonymizer._max_workers = original_max_workers

    assert serial_redacted == pooled_redacted
    assert serial_mapping == pooled_mapping


def test_repeated_batches_are_deterministic(anonymizer: PresidioAnonymizer) -> None:
    """Repeated runs of the same batch must yield the same mapping.

    Guards against cross-thread drift in the shared ``AnalyzerEngine``
    under the default (pooled) ``max_workers``.
    """
    first_redacted, first_mapping = anonymizer.anonymize_batch(_MULTI_LANGUAGE_BATCH)

    for _ in range(3):
        redacted, mapping = anonymizer.anonymize_batch(_MULTI_LANGUAGE_BATCH)
        assert redacted == first_redacted
        assert mapping == first_mapping


def test_every_configured_language_has_ner_and_lacks_parser(
    anonymizer: PresidioAnonymizer,
) -> None:
    """Pin the pipeline shape the speed-up depends on.

    Presidio reads ``doc.ents`` (``ner``); it never reads the dependency
    parse. A Presidio/spaCy upgrade that changes either would silently
    change detection quality or give back the CPU savings from dropping
    ``parser``.
    """
    for pairing in LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS:
        nlp = anonymizer._analyzer.nlp_engine.get_nlp(  # type: ignore[ty:unresolved-attribute]
            pairing["lang_code"]
        )
        assert "ner" in nlp.pipe_names
        assert "parser" not in nlp.pipe_names
