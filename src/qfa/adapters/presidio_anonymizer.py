"""Presidio-based anonymisation adapter.

Implements ``AnonymizationPort`` by delegating to Microsoft Presidio's
analyzer and anonymizer engines. Owns the heavy spaCy-backed pipelines
so the application service layer never imports Presidio directly.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException
from presidio_analyzer import AnalyzerEngine, BatchAnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine, OperatorConfig

from qfa.domain.ports import AnonymizationPort
from qfa.settings import (
    DEFAULT_ANONYMIZATION_BATCH_SIZE,
    DEFAULT_ANONYMIZATION_MAX_WORKERS,
)

# langdetect draws a random seed per process by default, so the same text can
# get different language guesses (and therefore different redactions) across
# calls. Pinning it makes detection deterministic — required for the
# worker-count-parity and repeat-determinism tests below to be a guarantee
# rather than a coincidence of which model happens to win on a given text.
DetectorFactory.seed = 0

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
    for a given set of detected entities is identical regardless of
    ``max_workers`` — see ``tests/adapters/test_presidio_anonymizer.py``.

    Every text also gets a second, ``xx``-model, ``PERSON``-only detection
    pass unioned onto the per-language model's result (PERSON wins any
    overlap) — the per-language models miss or mislabel some real names,
    which would otherwise leak verbatim. See :meth:`_merge_person_wins`.
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
        namespace stays serial and in input order, so output for a given
        set of detected entities is byte-identical to a fully serial run.
        Mappings from separate calls must not be merged — see
        :meth:`~qfa.domain.ports.AnonymizationPort.anonymize_batch`.

        Every text whose language isn't already ``xx`` also gets a second,
        ``xx``-model, ``PERSON``-only pass, unioned onto the first with
        PERSON winning any overlap — see :meth:`_merge_person_wins`. NER is
        statistical, so this reduces missed names, not eliminates them.
        """
        if not texts:
            return (), {}

        space = _PlaceholderSpace()
        languages = tuple(detect_language(text) for text in texts)
        results_by_index = self._run_pass(texts, languages)

        person_pass_indices = tuple(
            i for i, lang in enumerate(languages) if lang != "xx"
        )
        if person_pass_indices:
            person_pass_texts = tuple(texts[i] for i in person_pass_indices)
            person_pass_languages = tuple("xx" for _ in person_pass_indices)
            person_results = self._run_pass(
                person_pass_texts, person_pass_languages, entities=["PERSON"]
            )
            for local_index, global_index in enumerate(person_pass_indices):
                results_by_index[global_index] = self._merge_person_wins(
                    results_by_index[global_index], person_results[local_index]
                )

        redacted = tuple(
            self._redact(text, results, space)
            for text, results in zip(texts, results_by_index, strict=True)
        )
        return redacted, dict(space.mapping)

    def _run_pass(
        self,
        texts: tuple[str, ...],
        languages: tuple[str, ...],
        *,
        entities: list[str] | None = None,
    ) -> list[list[RecognizerResult]]:
        """Analyse ``texts`` (each against its ``languages`` entry), chunked and merged back.

        Chunked by language and, for more than one chunk, dispatched to
        the adapter's pool; a single chunk (one language, or a small
        batch) runs inline without touching the pool.
        """
        chunks = _plan_chunks(languages, self._max_workers)

        def analyze(
            indices: tuple[int, ...], language: str
        ) -> list[list[RecognizerResult]]:
            chunk_texts = [texts[i] for i in indices]
            if entities is None:
                return self._batch_analyzer.analyze_iterator(
                    chunk_texts, language=language, batch_size=self._batch_size
                )
            return self._batch_analyzer.analyze_iterator(
                chunk_texts,
                language=language,
                batch_size=self._batch_size,
                entities=entities,
            )

        results_by_index: list[list[RecognizerResult]] = [[] for _ in texts]
        if len(chunks) == 1:
            # Never empty: the caller guarantees non-empty texts, and
            # _plan_chunks emits >= 1 chunk per language. Run inline —
            # the single-text synchronous callers must never touch the pool.
            lang, indices = chunks[0]
            self._scatter(indices, analyze(indices, lang), results_by_index)
        else:
            futures = [
                (indices, self._pool.submit(analyze, indices, lang))
                for lang, indices in chunks
            ]
            for indices, future in futures:
                self._scatter(indices, future.result(), results_by_index)
        return results_by_index

    @staticmethod
    def _scatter(
        indices: tuple[int, ...],
        results: list[list[RecognizerResult]],
        results_by_index: list[list[RecognizerResult]],
    ) -> None:
        for index, result in zip(indices, results, strict=True):
            results_by_index[index] = result

    @staticmethod
    def _merge_person_wins(
        primary: list[RecognizerResult], person_pass: list[RecognizerResult]
    ) -> list[RecognizerResult]:
        """Union a ``PERSON``-only second pass onto ``primary``, PERSON winning overlaps.

        Adds non-overlapping ``person_pass`` spans to ``primary``'s PERSON
        spans, then drops any non-PERSON result overlapping the result —
        e.g. an ``ORGANIZATION`` mislabel of a name the second pass
        correctly tags. A union, not a replacement: a name only ``primary``
        catches is untouched.
        """

        def overlaps_any(
            candidate: RecognizerResult, spans: list[RecognizerResult]
        ) -> bool:
            return any(
                candidate.start < span.end and span.start < candidate.end
                for span in spans
            )

        persons = [result for result in primary if result.entity_type == "PERSON"]
        for candidate in person_pass:
            if not overlaps_any(candidate, persons):
                persons.append(candidate)

        non_person_survivors = [
            result
            for result in primary
            if result.entity_type != "PERSON" and not overlaps_any(result, persons)
        ]
        return persons + non_person_survivors

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
