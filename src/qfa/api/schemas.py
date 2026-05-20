"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def _assign_codes_request_examples() -> list[dict[str, Any]]:
    """Build Swagger ``examples`` from ``fixtures/coding_framework.json`` + COVID-19 codebook quotes."""
    root = Path(__file__).resolve().parents[3]
    path = root / "fixtures" / "coding_framework.json"
    if not path.is_file():
        return [
            {
                "coding_framework": {"types": []},
                "feedback_records": [
                    {
                        "id": "no-framework",
                        "content": (
                            "Repository root must contain fixtures/coding_framework.json "
                            "for full Try-it-out examples."
                        ),
                    }
                ],
                "max_codes": 10,
                "confidence_threshold": None,
            }
        ]
    # Dev-only: load JSON for Swagger examples; TODO: link production framework through API
    framework = json.loads(path.read_text(encoding="utf-8"))
    # Verbatim long examples from the COVID-19 coding framework (Excel export).
    quotes = [
        "they belief now a day covid-19 is as such not big deal, but the ruling party or the government used it as the agenda to divert the political view and opinion of the people towards the election after the coming two months",
        "This illness is creating a headache to us. We hear on the radio. All the things we used to help us we have stopped. We no longer travel to sell our things to other places. We are now hungry.",
        "transport is a very important pillar in the dvpt but the government should delimit areas of high contamination in order to limit movement to these areas",
    ]
    return [
        {
            "coding_framework": framework,
            "feedback_records": [
                {"id": f"covid-example-{i}", "content": text}
                for i, text in enumerate(quotes)
            ],
            "max_codes": 10,
            "confidence_threshold": None,
        }
    ]


class ApiFeedbackRecordInput(BaseModel):
    """A single feedback record in an analysis request."""

    id: str = Field(description="Unique identifier for the feedback record.")
    text: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback text content.",
    )
    metadata: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Optional metadata key-value pairs associated with the feedback record.",
    )


class ApiAnalyzeRequest(BaseModel):
    """Request body for the ``POST /v1/analyze`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "feedback_records": [
                        {
                            "id": "doc-001",
                            "text": "The water distribution was well organized but we had to wait for three hours.",
                            "metadata": {"region": "Eastern Province", "year": 2024},
                        },
                        {
                            "id": "doc-002",
                            "text": "Medical staff were very professional. Medicine supply was insufficient.",
                            "metadata": {"region": "Northern Province", "year": 2024},
                        },
                    ],
                    "prompt": "Summarize the main themes and sentiment of the feedback.",
                    "mode": "single_pass",
                },
            ],
        },
    }

    feedback_records: list[ApiFeedbackRecordInput] = Field(
        min_length=1,
        description="Non-empty list of feedback records to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4_000,
        description="Analysis instruction for the model.",
    )
    anonymize: bool = Field(
        default=True,
        description=(
            "If true, the service will anonymize feedback text before sending it"
            " to the LLM. Disable only if you are sure that no personally"
            " identifiable information (PII) is present in the input."
        ),
    )
    mode: Literal["single_pass"] = Field(
        default="single_pass",
        description=(
            "Analysis mode. ``single_pass`` is the only supported value in this"
            " version. Other modes (hierarchical / map-reduce) are tracked in #124."
        ),
    )


class ApiAnalyzeResponse(BaseModel):
    """Response body for the ``POST /v1/analyze`` endpoint.

    ``ai_generated`` and ``requires_human_review`` are constant ``true``
    on this endpoint by design — every analyse response is AI-generated
    and requires human review before action.
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
    ai_generated: bool = Field(
        description="Constant ``true`` — this endpoint always returns AI-generated content.",
    )
    requires_human_review: bool = Field(
        description="Constant ``true`` — every analyse response requires human review.",
    )
    feedback_record_count: int = Field(
        description="Number of feedback records that were analyzed.",
    )
    request_id: str = Field(description="Unique identifier for this request.")
    used_anonymization: bool = Field(
        description="Indicates whether anonymization was applied to the feedback text.",
    )


class ApiSummarizeFeedbackMetadata(BaseModel):
    """Metadata for a feedback record in a summarize request."""

    created: datetime = Field(
        description="Timestamp when the feedback record was created."
    )
    feedback_record_id: str = Field(description="Source feedback record identifier.")
    coding_level_1: str = Field(description="Level 1 coding label.")
    coding_level_2: str = Field(description="Level 2 coding label.")
    coding_level_3: str = Field(description="Level 3 coding label.")


class ApiSummarizeFeedbackRecord(BaseModel):
    """A single feedback record for ``POST /v1/summarize``."""

    id: str = Field(description="Unique identifier for the feedback record.")
    content: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback content to summarize.",
    )
    metadata: ApiSummarizeFeedbackMetadata = Field(
        description="Structured metadata for the feedback record.",
    )


class ApiSummarizeRequest(BaseModel):
    """Request body for the ``POST /v1/summarize`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "feedback_records": [
                        {
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
                        {
                            "id": "doc-002",
                            "content": (
                                "During the mobile clinic in the settlement after the floods, the "
                                "medical staff treated people with respect and explained things "
                                "clearly; several of us felt reassured even though we had waited most "
                                "of the morning in the heat. The nurses worked steadily and the "
                                "doctor listened properly before prescribing. What frustrated many "
                                "families was that essential medicines ran out before midday - "
                                "especially antibiotics and chronic medication for older people - so "
                                "some had to leave with prescriptions but no drugs, and others were "
                                "told to come back the next day without any guarantee that stock "
                                "would arrive. A few parents said their children's fever had still "
                                "not been checked by the time the team packed up. Overall the care "
                                "was professional, but unless supplies match the number of people, "
                                "the visit feels incomplete and people lose trust in follow-up."
                            ),
                            "metadata": {
                                "created": "2024-06-02T09:30:00Z",
                                "feedback_record_id": "fi-002",
                                "coding_level_1": "Health",
                                "coding_level_2": "Staff",
                                "coding_level_3": "Supplies",
                            },
                        },
                    ],
                    "output_language": "English",
                    "prompt": "Focus on operational issues and community-member experience.",
                },
            ],
        },
    }

    feedback_records: list[ApiSummarizeFeedbackRecord] = Field(
        min_length=1,
        description="Non-empty list of feedback records to summarize individually.",
    )
    output_language: str | None = Field(
        default=None,
        description="Optional target language for summaries and titles for every feedback record.",
    )
    prompt: str | None = Field(
        default=None,
        max_length=4_000,
        description="Optional extra instruction appended to the default summarize prompt.",
    )
    anonymize: bool = Field(
        default=True,
        description="If true, the service will anonymize feedback text before sending it to the LLM. Disable only if you are sure that no personally identifiable information (PII) is present in the input.",
    )


class ApiFeedbackRecordSummary(BaseModel):
    """Per-feedback-record summary response."""

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


class ApiSummarizeResponse(BaseModel):
    """Response body for the ``POST /v1/summarize`` endpoint."""

    summaries: list[ApiFeedbackRecordSummary] = Field(
        description="Title and summary for each submitted feedback record.",
    )
    used_anonymization: bool = Field(
        description="Indicates whether anonymization was applied to the feedback text.",
    )


class ApiAggregateSummary(BaseModel):
    """Aggregate summary covering all submitted feedback records."""

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


class ApiSummarizeAggregateResponse(BaseModel):
    """Response body for the ``POST /v1/summarize-aggregate`` endpoint."""

    summary: ApiAggregateSummary = Field(
        description="Aggregate summary of all submitted feedback records."
    )


class ApiCodingNode(BaseModel):
    """Contains the node of a singular coding and its' children."""

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


class ApiCodingLevels(BaseModel):
    """Contains the hierarchical codings used for classification."""

    root_codes: list[ApiCodingNode] = Field(
        description="The root (level 1) codes of your classification.", min_length=1
    )

    @model_validator(mode="after")
    def verify_all_codes_have_same_depth(self) -> "ApiCodingLevels":
        """Checks if all codes have the same depth."""
        max_lengths = set(code.max_child_depth() for code in self.root_codes)
        min_lengths = set(code.min_child_depth() for code in self.root_codes)
        if len(max_lengths.union(min_lengths)) > 1:
            raise ValueError(
                f"All codes must have the same depth {min_lengths=} {max_lengths=}"
            )

        return self


class ApiDetectSensitiveRequest(BaseModel):
    """Request body for the ``POST /v1/detect-sensitive`` endpoint.

    Attributes
    ----------
        feedback_items : list[ApiFeedbackRecordInput]
    """

    feedback_items: list[ApiFeedbackRecordInput] = Field(
        min_length=1,
        description="List of feedback items to check for sensitive content.",
    )

    anonymize: bool = Field(
        default=True,
        description="If true, the service will anonymize feedback text before sending it to the LLM. Disable only if you are sure that no personally identifiable information (PII) is present in the input.",
    )


class ApiFeedbackItemSensitivityRating(BaseModel):
    """Represents the sensitivity rating for a single feedback item.

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


class ApiDetectSensitiveResponse(BaseModel):
    """Response body for the ``POST /v1/detect-sensitive`` endpoint.

    Attributes
    ----------
    ratings : list[ApiFeedbackItemSensitivityRating]
        Sensitivity rating for each submitted feedback item.
    """

    ratings: list[ApiFeedbackItemSensitivityRating]


class ApiFeedbackRecord(BaseModel):
    """Feedback record: ``id`` plus body text (reusable across endpoints)."""

    id: str
    content: str = Field(min_length=1, max_length=100_000)


class ApiAssignCodesRequest(BaseModel):
    """Request body for ``POST /v1/assign_codes``."""

    model_config = {
        "json_schema_extra": {"examples": _assign_codes_request_examples()},
    }

    coding_framework: dict[str, Any]
    feedback_records: list[ApiFeedbackRecord] = Field(min_length=1)
    max_codes: int = Field(default=1, ge=1, le=50)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    anonymize: bool = Field(
        default=True,
        description="If true, the service will anonymize feedback text before sending it to the LLM. Disable only if you are sure that no personally identifiable information (PII) is present in the input.",
    )


class ApiAssignedCode(BaseModel):
    """A single code assigned to a feedback record."""

    code_id: str
    code_label: str
    confidence_type: float
    confidence_category: float
    confidence_code: float
    confidence_aggregate: float
    explanation: str


class ApiCodedFeedbackRecord(BaseModel):
    """The codes assigned to a single feedback record."""

    feedback_record_id: str
    assigned_codes: list[ApiAssignedCode]


class ApiAssignCodesResponse(BaseModel):
    """Response body for ``POST /v1/assign_codes``."""

    coded_feedback_records: list[ApiCodedFeedbackRecord]


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
