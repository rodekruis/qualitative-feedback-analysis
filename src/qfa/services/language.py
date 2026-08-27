"""Source-language detection for prompt assembly.

Wraps ``langdetect`` directly so the services layer can pin an LLM's output
language to the language a feedback record was written in. Deliberately
separate from :func:`qfa.adapters.presidio_anonymizer.detect_language`, which
answers a different question (which of Presidio's six spaCy models to load)
and would be an adapters import from the services layer besides.
"""

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

# ``langdetect`` samples randomly, so the same borderline text can yield
# different codes across calls. Seeding makes detection reproducible — note
# ``DetectorFactory`` is process-global, so this also pins the Presidio
# adapter's detection.
DetectorFactory.seed = 0

# Two independent gates, because neither catches the other's misses and a
# wrongly pinned language is worse than no directive at all (#294):
#
# - Below ~20 characters langdetect is confidently wrong ("Nobody helped me"
#   -> Welsh at p=0.99999), so no confidence threshold can filter it.
# - Above that length it still guesses, but hedges ("We need more blankets",
#   21 chars -> Afrikaans at p=0.57), which the length gate lets through.
#
# On short English feedback the observed probabilities cluster either at
# <=0.89 (hedged, usually wrong) or at >=0.9999 (correct), so 0.90 sits in a
# wide empty gap rather than on a cliff edge.
_MIN_DETECTION_CHARS = 20
_MIN_DETECTION_PROBABILITY = 0.90

# Feedback record content is capped at 100k characters; detection is
# synchronous CPU work on the event loop, and a 2k sample costs ~1.6ms
# against ~110ms for the full cap with no accuracy gained.
_DETECTION_SAMPLE_CHARS = 2000


def detect_source_language(text: str) -> str | None:
    """Detect the language ``text`` is written in.

    Returns a raw ISO 639-1 code (``"en"``, ``"fr"``), or ``None`` when the
    text is too short, too noisy, or too ambiguous to detect reliably —
    callers are expected to skip pinning a language in that case rather than
    trust a guess. Never raises. Only the first 2000 characters are inspected.
    """
    sample = text.strip()
    if len(sample) < _MIN_DETECTION_CHARS:
        return None
    try:
        best = detect_langs(sample[:_DETECTION_SAMPLE_CHARS])[0]
    except LangDetectException:
        return None
    if best.prob < _MIN_DETECTION_PROBABILITY:
        return None
    return best.lang
