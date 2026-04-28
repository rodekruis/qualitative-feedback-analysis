"""Presidio-based anonymisation adapter.

Implements ``AnonymizationPort`` by delegating to Microsoft Presidio's
analyzer and anonymizer engines. Owns the heavy spaCy-backed pipelines
so the application service layer never imports Presidio directly.
"""

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine, OperatorConfig


class PresidioAnonymizer:
    """``AnonymizationPort`` implementation backed by Presidio.

    Replaces detected entities with stable placeholders of the form
    ``<ENTITY_TYPE_N>`` (e.g. ``<PERSON_0>``, ``<LOCATION_1>``) so the
    same value gets the same placeholder within a single ``anonymize``
    call. ``DATE_TIME`` entities are preserved verbatim — they carry
    relevant context for analysis without identifying individuals.
    """

    def __init__(self) -> None:
        self._analyzer: AnalyzerEngine = AnalyzerEngine()
        self._anonymizer: AnonymizerEngine = AnonymizerEngine()

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace sensitive entities in ``text`` with placeholders."""
        mapping: dict[str, str] = {}

        results = self._analyzer.analyze(text=text, language="en")
        unique_entities = {res.entity_type for res in results}

        operators: dict[str, OperatorConfig] = {}
        for entity in unique_entities:
            operators[entity] = OperatorConfig(
                "custom",
                {
                    # Capture 'entity' as a default argument 'ent' to avoid closure issues
                    "lambda": lambda x, ent=entity: self._get_unique_id(x, ent, mapping)
                },
            )

        # Preserve DATE_TIME entities without anonymisation.
        operators["DATE_TIME"] = OperatorConfig("keep")

        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,  # type: ignore[arg-type]
            operators=operators,
        )
        return anonymized.text, mapping

    def deanonymize(self, text: str, mapping: dict[str, str]) -> str:
        """Restore original values in ``text`` using ``mapping``."""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text

    @staticmethod
    def _get_unique_id(
        original_value: str, entity_type: str, mapping: dict[str, str]
    ) -> str:
        """Return a stable placeholder for ``original_value`` within ``mapping``."""
        if original_value == "PII":
            return "<PII>"

        for placeholder, value in mapping.items():
            if value == original_value and placeholder.startswith(f"<{entity_type}_"):
                return placeholder

        placeholder = f"<{entity_type}_{len(mapping.keys())}>"
        mapping[placeholder] = original_value
        return placeholder
