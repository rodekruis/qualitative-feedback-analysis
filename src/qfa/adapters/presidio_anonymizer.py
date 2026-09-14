"""Presidio-based anonymisation adapter.

Implements ``AnonymizationPort`` by delegating to Microsoft Presidio's
analyzer and anonymizer engines. Owns the heavy spaCy-backed pipelines
so the application service layer never imports Presidio directly.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from langdetect import detect
from langdetect.lang_detect_exception import LangDetectException
from presidio_analyzer import AnalyzerEngine, BatchAnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine, OperatorConfig

from qfa.domain.ports import AnonymizationPort
from qfa.settings import (
    DEFAULT_ANONYMIZATION_BATCH_SIZE,
    DEFAULT_ANONYMIZATION_MAX_WORKERS,
)

LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS = [
    {"lang_code": "en", "model_name": "en_core_web_sm"},
    {"lang_code": "fr", "model_name": "fr_core_news_sm"},
    {"lang_code": "uk", "model_name": "uk_core_news_sm"},
    {"lang_code": "ru", "model_name": "ru_core_news_sm"},
    {"lang_code": "es", "model_name": "es_core_news_sm"},
    {"lang_code": "xx", "model_name": "xx_ent_wiki_sm"},
]

# spaCy's OntoNotes models emit entity labels that carry no PII meaning
# (numbers, facilities, products, etc.). Presidio has no mapping for them,
# so without this list it logs a "not mapped to a Presidio entity" warning
# for each one on every document. Ignoring them silences the noise *and*
# stops these tokens from being masked with spurious placeholders.
NON_PII_NER_LABELS_TO_IGNORE = [
    "CARDINAL",
    "ORDINAL",
    "QUANTITY",
    "PERCENT",
    "MONEY",
    "FAC",
    "PRODUCT",
    "EVENT",
    "WORK_OF_ART",
    "LAW",
    "LANGUAGE",
    "MISC",
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


def _balanced_contiguous_split(indices: list[int], n_parts: int) -> list[list[int]]:
    """Split ``indices`` into ``n_parts`` contiguous, near-equal groups."""
    base, extra = divmod(len(indices), n_parts)
    groups = []
    start = 0
    for part in range(n_parts):
        size = base + (1 if part < extra else 0)
        if size == 0:
            continue
        groups.append(indices[start : start + size])
        start += size
    return groups


def _plan_chunks(
    languages: tuple[str, ...], max_workers: int
) -> list[tuple[str, tuple[int, ...]]]:
    """Group text indices by language, then split each group for the pool.

    Each chunk is analysed by a single ``analyze_iterator`` call, which
    takes one language, so a chunk can never span languages. Within one
    language, indices are split into contiguous sub-chunks so the total
    chunk count stays close to ``max_workers`` — enough parallelism to use
    the whole pool without fragmenting the batch finer than it can run
    concurrently. A very skewed multi-language batch can land a few chunks
    over ``max_workers``; the pool bounds actual concurrency regardless, so
    that only costs a little dispatch overhead, not correctness.
    """
    groups: dict[str, list[int]] = {}
    for index, lang in enumerate(languages):
        groups.setdefault(lang, []).append(index)

    total = len(languages)
    chunks: list[tuple[str, tuple[int, ...]]] = []
    for lang, indices in groups.items():
        parts = max(1, min(len(indices), round(max_workers * len(indices) / total)))
        chunks.extend(
            (lang, tuple(group)) for group in _balanced_contiguous_split(indices, parts)
        )
    return chunks


@dataclass
class _PlaceholderSpace:
    """One placeholder namespace, shared by every text in a batch.

    Holds the mapping and the index counter together so they cannot
    drift apart: numbering off ``len(mapping)`` is what let placeholders
    collide across per-record calls (issue #324).
    """

    mapping: dict[str, str] = field(default_factory=dict)
    """Placeholder -> original value, in allocation order."""

    _by_value: dict[tuple[str, str], str] = field(default_factory=dict)
    """Reverse index (entity_type, original) -> placeholder.

    Not an optimisation: dedup by linear scan of ``mapping`` is
    O(entities²) over a request-wide namespace, and hierarchical analyse
    batches thousands of records.
    """

    _next_index: int = 0

    def placeholder_for(self, original_value: str, entity_type: str) -> str:
        """Return this namespace's placeholder for ``original_value``.

        Allocates a new one on first sight, otherwise reuses the existing
        placeholder for that (entity type, value) pair.
        """
        if original_value == "PII":
            return "<PII>"

        cached = self._by_value.get((entity_type, original_value))
        if cached is not None:
            return cached

        placeholder = f"<{entity_type}_{self._next_index}>"
        self._next_index += 1
        self.mapping[placeholder] = original_value
        self._by_value[entity_type, original_value] = placeholder
        return placeholder


class PresidioAnonymizer(AnonymizationPort):
    """``AnonymizationPort`` implementation backed by Presidio.

    Replaces detected entities with stable placeholders of the form
    ``<ENTITY_TYPE_N>`` (e.g. ``<PERSON_0>``, ``<LOCATION_1>``) so the
    same value gets the same placeholder within a single
    ``anonymize``/``anonymize_batch`` call — across every text in the
    batch. ``DATE_TIME`` entities are preserved verbatim — they carry
    relevant context for analysis without identifying individuals.

    Detection is batched through spaCy's ``nlp.pipe`` and parallelised
    across a bounded thread pool (spaCy/thinc releases the GIL during
    inference); placeholder allocation stays serial and in input order so
    ``_PlaceholderSpace`` is never touched by two threads at once. Output
    is identical to the fully serial implementation regardless of
    ``max_workers`` — see ``tests/adapters/test_presidio_anonymizer.py``.
    """

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_ANONYMIZATION_MAX_WORKERS,
        batch_size: int = DEFAULT_ANONYMIZATION_BATCH_SIZE,
    ) -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        self._analyzer: AnalyzerEngine = AnalyzerEngine(
            nlp_engine=NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS,
                    "ner_model_configuration": {
                        "labels_to_ignore": NON_PII_NER_LABELS_TO_IGNORE,
                    },
                }
            ).create_engine()
        )
        # Presidio reads doc.ents, tokens and lemmas; it never reads the
        # dependency parse. Dropping it is pure CPU savings (~25% of the
        # spaCy pass) with no effect on detection. Keep tagger/morphologizer,
        # attribute_ruler and lemmatizer: they feed the lemmas
        # LemmaContextAwareEnhancer uses to boost context-word matches.
        for pairing in LANGUAGES_AND_ANONYMIZATION_MODEL_PAIRINGS:
            # NlpEngine is the abstract base; get_nlp is SpacyNlpEngine-specific,
            # but that's the only engine this adapter configures.
            nlp = self._analyzer.nlp_engine.get_nlp(  # type: ignore[ty:unresolved-attribute]
                pairing["lang_code"]
            )
            if "parser" in nlp.pipe_names:
                nlp.remove_pipe("parser")
        self._batch_analyzer = BatchAnalyzerEngine(analyzer_engine=self._analyzer)
        self._anonymizer: AnonymizerEngine = AnonymizerEngine()
        self._max_workers = max_workers
        self._batch_size = batch_size
        # One long-lived pool per adapter instance. The composition root
        # builds exactly one PresidioAnonymizer, so this bounds total
        # Presidio detection threads across concurrent requests rather than
        # letting each request spawn its own.
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="presidio-anonymizer"
        )

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace sensitive entities in ``text`` with placeholders."""
        (redacted,), mapping = self.anonymize_batch((text,))
        return redacted, mapping

    def anonymize_batch(
        self, texts: tuple[str, ...]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Anonymise every text into one shared placeholder namespace.

        Detection (language + entity spans) is batched through
        ``nlp.pipe`` and, for a multi-chunk batch, parallelised across the
        adapter's thread pool. Allocation from the shared placeholder
        namespace stays serial and in input order, so output is
        byte-identical to a fully serial run. Mappings from separate calls
        must not be merged — see
        :meth:`~qfa.domain.ports.AnonymizationPort.anonymize_batch`.
        """
        if not texts:
            return (), {}

        space = _PlaceholderSpace()
        languages = tuple(detect_language(text) for text in texts)
        chunks = _plan_chunks(languages, self._max_workers)

        results_by_index: list[list[RecognizerResult]] = [[] for _ in texts]
        if len(chunks) <= 1:
            # Fast path for the single-text anonymize() used synchronously
            # by coding/summarize/sensitivity, and any small batch that
            # plans to a single chunk: run inline, never touch the pool.
            for lang, indices in chunks:
                self._scatter(
                    indices, self._analyze_chunk(texts, indices, lang), results_by_index
                )
        else:
            futures = [
                (indices, self._pool.submit(self._analyze_chunk, texts, indices, lang))
                for lang, indices in chunks
            ]
            for indices, future in futures:
                self._scatter(indices, future.result(), results_by_index)

        redacted = tuple(
            self._redact(text, results, space)
            for text, results in zip(texts, results_by_index, strict=True)
        )
        return redacted, dict(space.mapping)

    def _analyze_chunk(
        self, texts: tuple[str, ...], indices: tuple[int, ...], language: str
    ) -> list[list[RecognizerResult]]:
        """Run the batched analyser over one language chunk of ``texts``."""
        chunk_texts = [texts[i] for i in indices]
        return self._batch_analyzer.analyze_iterator(
            chunk_texts, language=language, batch_size=self._batch_size
        )

    @staticmethod
    def _scatter(
        indices: tuple[int, ...],
        results: list[list[RecognizerResult]],
        results_by_index: list[list[RecognizerResult]],
    ) -> None:
        for index, result in zip(indices, results, strict=True):
            results_by_index[index] = result

    def _redact(
        self, text: str, results: list[RecognizerResult], space: _PlaceholderSpace
    ) -> str:
        """Redact ``text`` given its pre-computed analyser ``results``."""
        unique_entities = {res.entity_type for res in results}

        operators: dict[str, OperatorConfig] = {}
        for entity in unique_entities:
            operators[entity] = OperatorConfig(
                "custom",
                {
                    # Capture 'entity' as a default argument 'ent' to avoid closure issues
                    "lambda": lambda x, ent=entity: space.placeholder_for(x, ent)
                },
            )

        # Preserve DATE_TIME entities without anonymisation.
        operators["DATE_TIME"] = OperatorConfig("keep")

        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,  # type: ignore[ty:invalid-argument-type]
            operators=operators,
        )
        return anonymized.text

    def deanonymize(self, text: str, mapping: dict[str, str]) -> str:
        """Restore original values in ``text`` using ``mapping``."""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text
