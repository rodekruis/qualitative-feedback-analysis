"""Tests for the Presidio-based anonymisation adapter."""

import pytest

from qfa.adapters.presidio_anonymizer import PresidioAnonymizer


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
