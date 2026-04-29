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
