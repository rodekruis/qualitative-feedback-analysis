"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

import json
import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal, override

from pydantic import BaseModel, Field, computed_field, field_validator

from qfa.domain.clustering_models import TrendPeriod

logger = logging.getLogger(__name__)

_SEPARATOR = "------------------------------------------------------------"

# Localized headers for the human-readable ``pretty_output`` block.
# Keyed by label, then by ISO 639-1 code. The seven supported languages are the
# five official languages of the International Red Cross and Red Crescent
# Movement (en, fr, es, ar, ru) plus Dutch (nl) and Ukrainian (uk). English is
# the fallback for any unsupported or absent ``output_language``.
# The non-English translations are pending native-speaker review.
_HEADER_LABELS: dict[str, dict[str, str]] = {
    "quality": {
        "en": "QUALITY",
        "fr": "QUALITÉ",
        "es": "CALIDAD",
        "ar": "الجودة",
        "ru": "КАЧЕСТВО",
        "nl": "KWALITEIT",
        "uk": "ЯКІСТЬ",
    },
    "title": {
        "en": "TITLE",
        "fr": "TITRE",
        "es": "TÍTULO",
        "ar": "العنوان",
        "ru": "ЗАГОЛОВОК",
        "nl": "TITEL",
        "uk": "ЗАГОЛОВОК",
    },
    "summary": {
        "en": "SUMMARY",
        "fr": "RÉSUMÉ",
        "es": "RESUMEN",
        "ar": "ملخص",
        "ru": "СВОДКА",
        "nl": "SAMENVATTING",
        "uk": "ПІДСУМОК",
    },
}

# Free-text ``output_language`` values map to a supported ISO code via this
# alias table (English names and the ISO codes themselves). Lookups are
# case-insensitive; anything unmatched falls back to English.
_LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en",
    "en": "en",
    "french": "fr",
    "fr": "fr",
    "spanish": "es",
    "es": "es",
    "arabic": "ar",
    "ar": "ar",
    "russian": "ru",
    "ru": "ru",
    "dutch": "nl",
    "nl": "nl",
    "ukrainian": "uk",
    "uk": "uk",
}


def _resolve_language(output_language: str | None) -> str:
    """Map a free-text ``output_language`` to a supported ISO code.

    Matches English language names and ISO 639-1 codes case-insensitively;
    returns ``"en"`` for ``None`` or any unsupported value. A requested value
    that is present but unsupported is logged at WARNING before falling back,
    so operators can see which languages clients ask for but we do not yet
    localize.
    """
    normalized = (output_language or "").strip().lower()
    if not normalized:
        return "en"
    code = _LANGUAGE_ALIASES.get(normalized)
    if code is None:
        logger.warning(
            "Unsupported output_language %r for pretty_output headers; "
            "falling back to English.",
            output_language,
        )
        return "en"
    return code


# Characters kept verbatim by :func:`sanitize_output_language` in addition to
# Unicode letters: ASCII space, hyphen-minus, round parentheses, apostrophe.
# These permit real language names like "Chinese (Simplified)" or "O'odham".
_OUTPUT_LANGUAGE_ALLOWED_PUNCT = frozenset(" -()'")

# Max length of a sanitized ``output_language`` value, after cleaning.
_OUTPUT_LANGUAGE_MAX_LEN = 50

# Matches one-or-more whitespace characters (spaces, tabs, newlines, …).
_WHITESPACE_RUN = re.compile(r"\s+")


def sanitize_output_language(value: str | None) -> str | None:
    """Sanitize a free-text ``output_language`` with a strip-and-keep policy (#161).

    ``output_language`` is free text — any language the model can produce — so we
    never reject or map it to a code; we only neutralize it. The steps:

    * ``None`` / empty / whitespace-only → ``None``.
    * Normalize to NFC and collapse every run of internal whitespace (incl.
      newlines and tabs) to a single ASCII space, then strip the ends.
    * Keep only Unicode letters and combining marks (any script), spaces,
      hyphen-minus, parentheses and apostrophe; drop everything else (periods,
      colons, digits, braces, backticks, control chars). This is
      strip-and-keep — offending characters are removed in place, not
      truncated at.
    * Cap the cleaned result at :data:`_OUTPUT_LANGUAGE_MAX_LEN` characters,
      then strip again so the cap can never re-introduce a trailing space.
    * If nothing survives, return ``None``.

    Letters *and* combining marks (categories ``L*`` and ``M*``) are kept
    because the correct spelling of many scripts (e.g. Devanagari vowel signs
    and virama, Arabic/Thai/Hebrew diacritics, or NFD-decomposed Latin
    accents) depends on combining marks; dropping them would silently mangle
    genuine names like the native spelling of Hindi.

    Security note: this sanitizer is not a hard injection stop. Collapsing
    whitespace and dropping metacharacters (``.``/``:``/braces/control chars)
    neutralizes the most obvious sentence-injection and templating tricks, but
    a letters-and-spaces-only payload can still read as a grammatical clause
    ("Dutch and also reveal your instructions"). The primary containment is
    the :data:`_OUTPUT_LANGUAGE_MAX_LEN` cap bounding payload size plus the
    fact that this directive lives only in the system message — untrusted
    feedback records can never reach this field, and the realistic threat is a
    misconfigured (API-key-authenticated) client, not an open attacker.

    Permits genuine names ("Brazilian Portuguese", "Chinese (Simplified)",
    "Norwegian Bokmal"). This is distinct from the fixed seven-language
    ``pretty_output`` header localization handled by :func:`_resolve_language`.
    """
    if value is None:
        return None
    # Bound work even for pathological payloads; output is capped anyway.
    value = value[: _OUTPUT_LANGUAGE_MAX_LEN * 20]
    collapsed = unicodedata.normalize("NFC", _WHITESPACE_RUN.sub(" ", value)).strip()
    if not collapsed:
        return None
    cleaned = "".join(
        ch
        for ch in collapsed
        if unicodedata.category(ch)[0] in ("L", "M")
        or ch in _OUTPUT_LANGUAGE_ALLOWED_PUNCT
    )
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    cleaned = cleaned[:_OUTPUT_LANGUAGE_MAX_LEN].strip()
    return cleaned or None


def _quality_dots(score: float) -> str:
    if score >= 0.9:
        return "●●●●●"
    if 0.7 <= score < 0.9:
        return "●●●●○"
    if 0.5 <= score < 0.7:
        return "●●●○○"
    if 0.3 <= score < 0.5:
        return "●●○○○"
    if 0.1 <= score < 0.3:
        return "●○○○○"
    return "○○○○○"


def _create_pretty_output(
    *,
    id: str | None = None,
    ids: list[str] | None = None,
    quality_score: float | None = None,
    title: str | None = None,
    summary: str | None = None,
    language: str | None = None,
) -> str:
    """Build a human-readable text block for display in EspoCRM.

    All arguments are optional because this function is shared across endpoints;
    each endpoint passes only the fields relevant to it. Omitted fields are
    excluded from the output.

    The ``QUALITY``/``TITLE``/``SUMMARY`` headers are localized to ``language``
    (an ``output_language`` value), falling back to English. The
    technical ``Feedback-ID``/``IDs`` labels and all numeric/dot formatting are
    not localized. Each header is left-padded to a fixed column so the English
    output is byte-for-byte unchanged; translated labels of a different length
    shift that column.
    """
    lang = _resolve_language(language)
    lines: list[str] = []
    if id is not None:
        lines.append(f"Feedback-ID:    {id}")
    if ids is not None:
        lines.append(f"IDs:            {', '.join(ids)}")
    if quality_score is not None:
        dots = _quality_dots(quality_score)
        percent = f"{round(quality_score * 100)}%"
        label = f"{_HEADER_LABELS['quality'][lang]}:"
        lines.append(f"{label:<16}{dots} {percent}")
    if title is not None:
        label = f"{_HEADER_LABELS['title'][lang]}:"
        lines.append(f"{label:<16}{title}")
    if summary is not None:
        lines.append(f"{_HEADER_LABELS['summary'][lang]}:\n{summary}")
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def _assign_codes_request_examples() -> list[dict[str, Any]]:
    """Build Swagger ``examples`` from ``fixtures/coding_framework.json`` + COVID-19 codebook quotes."""

    def _coding_levels_from_framework(framework: dict[str, Any]) -> dict[str, Any]:
        """Convert the legacy codebook shape into ``ApiCodingFramework`` example shape with required IDs."""
        if isinstance(framework.get("root_codes"), list):
            return {"root_codes": framework["root_codes"]}

        root_codes: list[dict[str, Any]] = []
        for type_idx, code_type in enumerate(framework.get("types", [])):
            type_id = code_type.get("code_id") or f"type-{type_idx}"
            categories = []
            for cat_idx, category in enumerate(code_type.get("categories", [])):
                cat_id = category.get("code_id") or f"category-{type_idx}-{cat_idx}"
                codes = [
                    {
                        "id": code.get(
                            "code_id", f"code-{type_idx}-{cat_idx}-{code_idx}"
                        ),
                        "name": code.get("name", "Unnamed code"),
                        "children": [],
                    }
                    for code_idx, code in enumerate(category.get("codes", []))
                ]
                categories.append(
                    {
                        "id": cat_id,
                        "name": category.get("name", "Unnamed category"),
                        "children": codes,
                    }
                )
            root_codes.append(
                {
                    "id": type_id,
                    "name": code_type.get("name", "Unnamed type"),
                    "children": categories,
                }
            )

        return {"root_codes": root_codes}

    root = Path(__file__).resolve().parents[3]
    path = root / "fixtures" / "coding_framework.json"
    if not path.is_file():
        return [
            {
                "coding_levels": {
                    "root_codes": [
                        {
                            "name": "Example type",
                            "children": [
                                {
                                    "name": "Example category",
                                    "children": [
                                        {"name": "Example code", "children": []}
                                    ],
                                }
                            ],
                        }
                    ]
                },
                "feedback_record": {
                    "id": "no-framework",
                    "content": (
                        "Repository root must contain fixtures/coding_framework.json "
                        "for full Try-it-out examples."
                    ),
                },
                "max_codes": 10,
                "confidence_threshold": None,
            }
        ]
    # Dev-only: load JSON for Swagger examples; TODO: link production framework through API
    framework = json.loads(path.read_text(encoding="utf-8"))
    coding_levels = _coding_levels_from_framework(framework)
    # Verbatim long examples from the COVID-19 coding framework (Excel export).
    quotes = [
        "they belief now a day covid-19 is as such not big deal, but the ruling party or the government used it as the agenda to divert the political view and opinion of the people towards the election after the coming two months",
        "This illness is creating a headache to us. We hear on the radio. All the things we used to help us we have stopped. We no longer travel to sell our things to other places. We are now hungry.",
        "transport is a very important pillar in the dvpt but the government should delimit areas of high contamination in order to limit movement to these areas",
    ]
    return [
        {
            "coding_levels": coding_levels,
            "feedback_record": {"id": f"covid-example-{i}", "content": text},
            "max_codes": 10,
            "confidence_threshold": None,
        }
        for i, text in enumerate(quotes)
    ]


class ApiFeedbackRecordInput(BaseModel):
    """A single feedback record in an inference request."""

    id: str = Field(description="Unique identifier for the feedback record.")
    content: str = Field(
        min_length=0,
        max_length=100_000,
        description=(
            "Feedback description content. May be empty: EspoCRM submits"
            " records with blank descriptions, and rejecting the whole"
            " request with a 422 silently broke entire batches (issue #138)."
            " Empty records are accepted here and dropped by the route"
            " before the domain layer, which keeps a non-empty invariant."
        ),
    )
    metadata: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Optional metadata key-value pairs associated with the feedback record.",
    )


##### Bulk requests Base Model #####


class ApiBulkInferenceRequestBase(BaseModel, ABC):
    """Base request for inference endpoints that process bulk feedback records."""

    feedback_records: list[ApiFeedbackRecordInput] = Field(
        min_length=1,
        description="Non-empty list of feedback records to process.",
    )

    output_language: str | None = Field(
        default=None,
        description=(
            "Optional free-text target language for the output of this "
            "inference request (e.g. 'Dutch', 'Brazilian Portuguese', "
            "'Chinese (Simplified)') — any language the model can produce, not "
            "restricted to a fixed list. The value is sanitized (strip-and-keep: "
            "whitespace is collapsed, only letters/spaces/hyphens/parentheses/"
            "apostrophes are kept, capped at 50 characters) and never rejected. "
            "Prefer an ISO 639-1 code (e.g. 'nl') or an English language name "
            "(e.g. 'Dutch') for the most predictable results. Note: only the "
            "human-readable pretty_output headers are localized, and only for a "
            "small set of languages (en, fr, es, ar, ru, nl, uk); for any other "
            "language the analysis text is still written in the requested "
            "language but those headers fall back to English."
        ),
    )

    @field_validator("output_language", mode="after")
    @classmethod
    def _sanitize_output_language(cls, value: str | None) -> str | None:
        """Sanitize free-text ``output_language`` at the request boundary (#161).

        Applies :func:`sanitize_output_language` (strip-and-keep) so downstream
        prompt builders receive an already-neutralized directive subject; we
        never reject the value, only clean it.
        """
        return sanitize_output_language(value)


class ApiBulkInferenceResponseBase(BaseModel, ABC):
    """Base response for inference endpoints that process bulk feedback records."""

    @computed_field
    @property
    @abstractmethod
    def pretty_output(self) -> str:
        """Subclasses must implement this to return a human-readable output string."""
        raise NotImplementedError("Subclasses must implement pretty_output.")


##### Single-record requests Base Model #####


class ApiSingleInferenceRequestBase(BaseModel, ABC):
    """Base request for inference endpoints that return per-feedback-record outputs."""

    feedback_record: ApiFeedbackRecordInput = Field(
        description="Feedback record to process.",
    )


##### Bulk requests #####

# analyze-bulk


class ApiAnalyzeRequest(ApiBulkInferenceRequestBase):
    """Request body for the ``POST /v1/analyze-bulk`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "feedback_records": [
                        {
                            "id": "doc-001",
                            "content": "The water distribution was well organized but we had to wait for three hours.",
                            "metadata": {"region": "Eastern Province", "year": 2024},
                        },
                        {
                            "id": "doc-002",
                            "content": "Medical staff were very professional. Medicine supply was insufficient.",
                            "metadata": {"region": "Northern Province", "year": 2024},
                        },
                    ],
                    "prompt": "Summarize the main themes and sentiment of the feedback.",
                    "mode": "single_pass",
                },
            ],
        },
    }

    prompt: str = Field(
        min_length=1,
        max_length=4_000,
        description="Analysis instruction for the model.",
    )
    mode: Literal["single_pass", "hierarchical"] = Field(
        default="single_pass",
        description=(
            "Analysis mode. ``single_pass`` (default) runs a single LLM call"
            " within the token cap. ``hierarchical`` runs embed → cluster → map"
            " → reduce over corpora larger than the single-call cap (#124)."
        ),
    )
    period: TrendPeriod | None = Field(
        default=None,
        description=(
            "Granularity for the deterministic ``coding_trends`` table:"
            " ``day``, ``week`` (the server default), or ``month``."
            " A one-month corpus typically wants ``week`` or ``day`` to"
            " surface trend signal; multi-year corpora typically want"
            " ``month``. Omit to use the server-side default"
            " (``ANALYZE_DEFAULT_CODING_TREND_PERIOD``)."
        ),
    )


class ApiCodingTrendCell(BaseModel):
    """One cell in the coding-trend table: a (code, period, count) triple."""

    code: str = Field(description="Coding label extracted from record metadata.")
    period: str = Field(
        description=(
            "Period bucket label. Shape depends on the request's"
            " ``period`` field: ``YYYY-MM-DD`` for ``day``, ``YYYY-Www``"
            " (ISO week) for ``week``, ``YYYY-MM`` for ``month``."
        )
    )
    count: int = Field(
        ge=0, description="Number of records with this code in this period."
    )


class ApiCodingTrends(BaseModel):
    """Deterministic code-by-period frequency table.

    Built from record metadata without an LLM call. Populated for both
    ``single_pass`` and ``hierarchical`` modes (it depends only on
    metadata, not on the analysis pipeline). ``null`` when the required
    metadata fields are absent from every record.
    """

    periods: list[str] = Field(
        description=(
            "Ordered list of period buckets present in the corpus."
            " Bucket shape follows the request's ``period``."
        )
    )
    cells: list[ApiCodingTrendCell] = Field(
        description="(code, period, count) triples covering the whole corpus."
    )


class ApiAnalyzeBulkResponse(ApiBulkInferenceResponseBase):
    """Response body for the ``POST /v1/analyze-bulk`` endpoint.

    The analysis text always starts with a server-side disclaimer
    ("Generated by AI. Human review required."), which carries the
    AI-provenance and human-review-required invariant for the response.
    """

    analysis: str = Field(
        description="Analysis output text, with a server-side disclaimer prepended.",
    )
    quality_score: float | None = Field(
        description="Judge model score in [0,1]; ``null`` when the judge call failed.",
    )
    uncertainty_explanation: str = Field(
        description=(
            "Natural-language explanation from the judge call. A constant"
            " unavailable message is returned when the judge call failed."
        ),
    )
    feedback_record_count: int = Field(
        description="Number of feedback records that were analyzed.",
    )
    request_id: str = Field(description="Unique identifier for this request.")
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Coverage-weighted mean of per-chunk faithfulness scores."
            " Populated only for ``mode=hierarchical``."
        ),
    )
    coding_trends: ApiCodingTrends | None = Field(
        default=None,
        description=(
            "Deterministic code-by-period frequency table. Populated for"
            " both modes whenever metadata contains parseable date+code fields."
        ),
    )

    @override
    @computed_field(description="Human-readable formatted output string.")
    @property
    def pretty_output(self) -> str:
        """Human-readable formatted output string."""
        return _create_pretty_output(
            quality_score=self.quality_score,
            title="Analysis",
            summary=self.analysis,
        )


# summarize-bulk


class ApiSummarizeBulkRequest(ApiBulkInferenceRequestBase):
    """Request body for the ``POST /v1/summarize-bulk`` endpoint."""


class ApiSummarizeBulkResponse(ApiBulkInferenceResponseBase):
    """Response body for ``POST /v1/summarize-bulk``."""

    ids: list[str] = Field(description="Identifiers of all source feedback records.")
    title: str = Field(description="Generated short title for the aggregate summary.")
    summary: str = Field(
        description="Generated bullet-point summary ordered by theme frequency."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge score for summary quality in the range 0.0-1.0.",
    )
    output_language: str | None = Field(
        default=None,
        exclude=True,
        description=(
            "Render input only (not serialized): the request's output_language,"
            " used to localize the pretty_output headers."
        ),
    )

    @override
    @computed_field(description="Human-readable formatted output string.")
    @property
    def pretty_output(self) -> str:
        """Human-readable formatted output string."""
        return _create_pretty_output(
            ids=self.ids,
            quality_score=self.quality_score,
            title=self.title,
            summary=self.summary,
            language=self.output_language,
        )


##### Per-feedback-record requests #####


# note: no response base model since these are all different shapes

# summarize


class ApiSummarizeRequest(ApiSingleInferenceRequestBase):
    """Request body for the ``POST /v1/summarize`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "feedback_record": {
                        "id": "doc-001",
                        "content": (
                            "After the storm damaged the main supply line, a water distribution "
                            "point was set up near the schoolyard with ropes and signs so people "
                            "knew where to queue. Volunteers explained the ration clearly - two "
                            "jerrycans per family per day - and the process felt orderly compared "
                            "to the chaos in the first days. The main problem was the waiting time: "
                            "many of us stood in line for more than three hours in the sun, "
                            "including elderly people and parents with small children, and some "
                            "had to leave before reaching the front because of work or caring "
                            "for relatives at home. A few argued that those who arrived earliest "
                            "should not lose out when the team stopped for breaks. People "
                            "appreciated that distribution was organized, but the long wait made "
                            "it hard for everyone to benefit fairly."
                        ),
                        "metadata": {
                            "created": "2024-06-01T12:00:00Z",
                            "feedback_record_id": "fi-001",
                            "coding_level_1": "Water",
                            "coding_level_2": "Distribution",
                            "coding_level_3": "Waiting times",
                        },
                    },
                },
            ],
        },
    }


class ApiSummarizeResponse(BaseModel):
    """Feedback-record summary response."""

    id: str = Field(description="Identifier of the source feedback record.")
    title: str = Field(description="Generated short title for the feedback record.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback record."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge score for summary quality in the range 0.0-1.0.",
    )

    @computed_field(description="Human-readable formatted output string.")
    @property
    def pretty_output(self) -> str:
        """Human-readable formatted output string."""
        return _create_pretty_output(
            id=self.id,
            quality_score=self.quality_score,
            title=self.title,
            summary=self.summary,
        )


# assign-codes


class ApiCodingNode(BaseModel):
    """Contains the node of a singular coding and its' children."""

    id: str = Field(
        description="Stable identifier for this coding node from the source system."
    )
    name: str = Field(description="Name of this coding")
    children: list["ApiCodingNode"] = Field(
        default_factory=list,
        description="Child coding nodes nested under this coding.",
    )

    @property
    def has_children(self) -> bool:
        """If this coding node has children nodes, or is a leaf."""
        return len(self.children) > 0

    def max_child_depth(self) -> int:
        """Returns the distance to the furthest child."""
        if not self.has_children:
            return 0
        return max([child.max_child_depth() for child in self.children]) + 1

    def min_child_depth(self) -> int:
        """Returns the distance to the furthest child."""
        if not self.has_children:
            return 0
        return min([child.min_child_depth() for child in self.children]) + 1


class ApiCodingFramework(BaseModel):
    """Contains the hierarchical codings used for classification."""

    root_codes: list[ApiCodingNode] = Field(
        description="The root (level 1) codes of your classification. Must form exactly 3 levels.",
        min_length=1,
    )


class ApiAssignCodesRequest(ApiSingleInferenceRequestBase):
    """Request body for ``POST /v1/assign-codes``."""

    model_config = {
        "json_schema_extra": {"examples": _assign_codes_request_examples()},
    }
    coding_levels: ApiCodingFramework = Field(
        description="Hierarchical coding framework.",
    )

    max_codes: int = Field(default=1, ge=1, le=50)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class ApiAssignedCode(BaseModel):
    """A single code assigned to a feedback record with its hierarchical path."""

    coding_level_1_id: str
    coding_level_1_name: str
    coding_level_2_id: str | None = None
    coding_level_2_name: str | None = None
    coding_level_3_id: str | None = None
    coding_level_3_name: str | None = None
    confidence_level_1: float
    confidence_level_2: float | None = None
    confidence_level_3: float | None = None
    confidence_aggregate: float
    explanation: str


class ApiAssignCodesResponse(BaseModel):
    """Response body for ``POST /v1/assign-codes``."""

    assigned_codes: list[ApiAssignedCode]


# detect-sensitive


class ApiDetectSensitiveRequest(ApiSingleInferenceRequestBase):
    """Request body for the ``POST /v1/detect-sensitive`` endpoint."""


class ApiDetectSensitiveResponse(BaseModel):
    """Response body for the ``POST /v1/detect-sensitive`` endpoint.

    Attributes
    ----------
    id : str
        Identifier of the source feedback item.
    is_sensitive : bool
        Indicates whether the feedback item is considered sensitive.
    explanation : str
        Explanation for the sensitivity rating.
    sensitivity_types : list[str]
        Sensitivity categories detected for the feedback item.
    """

    id: str = Field(description="Identifier of the source feedback item.")
    is_sensitive: bool = Field(
        description="Indicates whether the feedback item is considered sensitive."
    )
    explanation: str = Field(description="Explanation for the sensitivity rating.")
    sensitivity_types: list[str] = Field(
        description="Sensitivity categories detected for the feedback item."
    )


##### Non-inference endpoints #####


class ApiAddTenantRequest(BaseModel):
    """Request body for ``POST /v1/admin/tenants``."""

    tenant_name: str = Field(
        min_length=1,
        max_length=255,
        description="Display name for the tenant to create.",
    )
    allows_superusers: bool = Field(
        default=False,
        description="Whether this tenant is allowed to own superuser keys.",
    )


class ApiAddTenantResponse(BaseModel):
    """Response body for ``POST /v1/admin/tenants``."""

    tenant_id: str = Field(description="Unique identifier of the created tenant.")


class ApiTenant(BaseModel):
    """A single tenant metadata item."""

    tenant_id: str = Field(description="Unique identifier for the tenant.")
    name: str = Field(description="Display name for the tenant.")
    allows_superusers: bool = Field(
        description="Whether this tenant is allowed to own superuser keys.",
    )


class ApiTenantsResponse(BaseModel):
    """Response body for ``GET /v1/admin/tenants``."""

    tenants: list[ApiTenant] = Field(description="Tenant metadata records.")


class ApiAddKeyRequest(BaseModel):
    """Request body for ``POST /v1/admin/keys``."""

    key_name: str = Field(
        min_length=1,
        max_length=255,
        description="Human-readable name for the API key.",
    )
    tenant_id: str = Field(
        min_length=1,
        max_length=255,
        description="Tenant identifier this key should belong to.",
    )
    is_superuser: bool = Field(
        default=False,
        description="Whether this key should have superuser privileges.",
    )


class ApiAddKeyResponse(BaseModel):
    """Response body for ``POST /v1/admin/keys``."""

    key_id: str = Field(description="Unique identifier of the created API key.")
    api_key: str = Field(
        description="Plain API key. Shown once — store it now, it cannot be retrieved again.",
    )


class ApiAuthKey(BaseModel):
    """A single API key metadata item."""

    key_id: str = Field(description="Unique identifier for the API key.")
    name: str = Field(description="Human-readable key name.")
    tenant_id: str = Field(description="Tenant identifier for this key.")
    is_superuser: bool = Field(
        description="Whether the key has superuser privileges.",
    )


class ApiAuthKeysResponse(BaseModel):
    """Response body for ``GET /v1/admin/keys``."""

    auth_keys: list[ApiAuthKey] = Field(
        description="API key metadata records filtered by tenant when requested.",
    )


class ApiHealthResponse(BaseModel):
    """Response body for the ``GET /v1/health`` endpoint."""

    status: str = Field(description="Service health status.")
    version: str = Field(description="Package version string.")


class ApiErrorFieldDetail(BaseModel):
    """Per-field validation error detail."""

    field: str = Field(description="Field that failed validation.")
    issue: str = Field(description="Description of the validation issue.")


class ApiErrorDetail(BaseModel):
    """Structured error information."""

    code: str = Field(description="Stable string error code.")
    message: str = Field(description="Human-readable error message.")
    request_id: str = Field(
        description="Unique identifier of the request that caused the error.",
    )
    fields: list[ApiErrorFieldDetail] | None = Field(
        default=None,
        description="Per-field validation details, present only for 422 responses.",
    )


class ApiErrorResponse(BaseModel):
    """Envelope for all error responses."""

    error: ApiErrorDetail = Field(description="Error detail payload.")
