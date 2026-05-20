"""Presidio-based anonymisation adapter.

Implements ``AnonymizationPort`` by delegating to Microsoft Presidio's
analyzer and anonymizer engines. Owns the heavy spaCy-backed pipelines
so the application service layer never imports Presidio directly.
"""

from langdetect import detect
from langdetect.lang_detect_exception import LangDetectException
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine, OperatorConfig

from qfa.domain.ports import AnonymizationPort

LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS = [
    {"lang_code": "en", "model_name": "en_core_web_sm"},
    {"lang_code": "fr", "model_name": "fr_core_news_sm"},
    {"lang_code": "uk", "model_name": "uk_core_news_sm"},
    {"lang_code": "ru", "model_name": "ru_core_news_sm"},
    {"lang_code": "es", "model_name": "es_core_news_sm"},
    {"lang_code": "xx", "model_name": "xx_ent_wiki_sm"},
]


def detect_language(text: str) -> str:
    """Detect the language of ``text`` and return a Presidio-compatible language code.

    If the detected language isn't in our pre-defined list of SpaCy models, return "xx".
    """
    try:
        language_shortcode = detect(text)
    except LangDetectException:
        return "xx"

    if language_shortcode not in [
        ent["lang_code"] for ent in LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS
    ]:
        return "xx"
    return language_shortcode


class PresidioAnonymizer(AnonymizationPort):
    """``AnonymizationPort`` implementation backed by Presidio.

    Replaces detected entities with stable placeholders of the form
    ``<ENTITY_TYPE_N>`` (e.g. ``<PERSON_0>``, ``<LOCATION_1>``) so the
    same value gets the same placeholder within a single ``anonymize``
    call. ``DATE_TIME`` entities are preserved verbatim — they carry
    relevant context for analysis without identifying individuals.
    """

    def __init__(self) -> None:
        self._analyzer: AnalyzerEngine = AnalyzerEngine(
            nlp_engine=NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS,
                }
            ).create_engine()
        )
        self._anonymizer: AnonymizerEngine = AnonymizerEngine()

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace sensitive entities in ``text`` with placeholders."""
        detected_language = detect_language(text)

        mapping: dict[str, str] = {}
        results = self._analyzer.analyze(text=text, language=detected_language)
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
            analyzer_results=results,  # type: ignore[ty:invalid-argument-type]
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
