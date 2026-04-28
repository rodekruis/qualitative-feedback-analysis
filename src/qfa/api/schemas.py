"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_serializer, model_validator


def _assign_codes_request_examples() -> list[dict[str, Any]]:
    """Build Swagger ``examples`` from ``fixtures/coding_framework.json`` + COVID-19 codebook quotes."""
    root = Path(__file__).resolve().parents[3]
    path = root / "fixtures" / "coding_framework.json"
    if not path.is_file():
        return [
            {
                "coding_framework": {"types": []},
                "feedback_items": [
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
            "feedback_items": [
                {"id": f"covid-example-{i}", "content": text}
                for i, text in enumerate(quotes)
            ],
            "max_codes": 10,
            "confidence_threshold": None,
        }
    ]


class FeedbackItemInput(BaseModel):
    """A single feedback item in an analysis request."""

    id: str = Field(description="Unique identifier for the feedback item.")
    text: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback text content.",
    )
    metadata: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Optional metadata key-value pairs associated with the feedback item.",
    )


class AnalyzeRequest(BaseModel):
    """Request body for the ``POST /v1/analyze`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "documents": [
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
                },
            ],
        },
    }

    documents: list[FeedbackItemInput] = Field(
        min_length=1,
        description="Non-empty list of feedback items to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4_000,
        description="Analysis instruction for the model.",
    )
    deactivate_anonymization: bool = Field(
        default=False,
        description="If true, the service will not apply anonymization to the feedback text. Use with caution and only if you are sure that no personally identifiable information (PII) is present in the input.",
    )


class AnalyzeResponse(BaseModel):
    """Response body for the ``POST /v1/analyze`` endpoint."""

    analysis: str = Field(description="Analysis output text.")
    document_count: int = Field(description="Number of documents that were analyzed.")
    request_id: str = Field(description="Unique identifier for this request.")
    used_anonymization: bool = Field(
        description="Indicates whether anonymization was applied to the feedback text."
    )


class SummarizeFeedbackMetadata(BaseModel):
    """Metadata for a feedback item in a summarize request."""

    created: datetime = Field(
        description="Timestamp when the feedback item was created."
    )
    feedback_item_id: str = Field(description="Source feedback item identifier.")
    coding_level_1: str = Field(description="Level 1 coding label.")
    coding_level_2: str = Field(description="Level 2 coding label.")
    coding_level_3: str = Field(description="Level 3 coding label.")


class SummarizeFeedbackItem(BaseModel):
    """A single feedback item for ``POST /v1/summarize``."""

    id: str = Field(description="Unique identifier for the feedback item.")
    content: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback content to summarize.",
    )
    metadata: SummarizeFeedbackMetadata = Field(
        description="Structured metadata for the feedback item.",
    )


class SummarizeRequest(BaseModel):
    """Request body for the ``POST /v1/summarize`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "feedback_items": [
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
                                "feedback_item_id": "fi-001",
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
                                "feedback_item_id": "fi-002",
                                "coding_level_1": "Health",
                                "coding_level_2": "Staff",
                                "coding_level_3": "Supplies",
                            },
                        },
                    ],
                    "output_language": "English",
                    "prompt": "Focus on operational issues and beneficiary experience.",
                },
            ],
        },
    }

    feedback_items: list[SummarizeFeedbackItem] = Field(
        min_length=1,
        description="Non-empty list of feedback items to summarize individually.",
    )
    output_language: str | None = Field(
        default=None,
        description="Optional target language for summaries and titles for every item.",
    )
    prompt: str | None = Field(
        default=None,
        max_length=4_000,
        description="Optional extra instruction appended to the default summarize prompt.",
    )
    deactivate_anonymization: bool = Field(
        default=False,
        description="If true, the service will not apply anonymization to the feedback text. Use with caution and only if you are sure that no personally identifiable information (PII) is present in the input.",
    )


class FeedbackItemSummary(BaseModel):
    """Summary response item for a single feedback item."""

    id: str = Field(description="Identifier of the source feedback item.")
    title: str = Field(description="Generated short title for the feedback item.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback item."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge score for summary quality in the range 0.0-1.0.",
    )


class SummarizeResponse(BaseModel):
    """Response body for the ``POST /v1/summarize`` endpoint."""

    summaries: list[FeedbackItemSummary] = Field(
        description="Title and summary for each submitted feedback item.",
    )
    used_anonymization: bool = Field(
        description="Indicates whether anonymization was applied to the feedback text.",
    )


class AggregateSummary(BaseModel):
    """Aggregate summary covering all submitted feedback items."""

    ids: list[str] = Field(description="Identifiers of all source feedback items.")
    title: str = Field(description="Generated short title for the aggregate summary.")
    summary: str = Field(
        description="Generated bullet-point summary ordered by theme frequency."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge score for summary quality in the range 0.0-1.0.",
    )


class SummarizeAggregateResponse(BaseModel):
    """Response body for the ``POST /v1/summarize-aggregate`` endpoint."""

    summary: AggregateSummary = Field(
        description="Aggregate summary of all submitted feedback items."
    )


class CodingNode(BaseModel):
    """Contains the node of a singular coding and its' children."""

    name: str = Field(description="Name of this coding")
    children: list["CodingNode"] = Field(
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


class CodingLevels(BaseModel):
    """Contains the hierarchical codings used for classification."""

    root_codes: list[CodingNode] = Field(
        description="The root (level 1) codes of your classification.", min_length=1
    )

    @model_validator(mode="after")
    def verify_all_codes_have_same_depth(self) -> "CodingLevels":
        """Checks if all codes have the same depth."""
        max_lengths = set(code.max_child_depth() for code in self.root_codes)
        min_lengths = set(code.min_child_depth() for code in self.root_codes)
        if len(max_lengths.union(min_lengths)) > 1:
            raise ValueError(
                f"All codes must have the same depth {min_lengths=} {max_lengths=}"
            )

        return self


class FeedbackItem(BaseModel):
    """Feedback item: ``id`` plus body text (reusable across endpoints)."""

    id: str
    content: str = Field(min_length=1, max_length=100_000)


class AssignCodesRequest(BaseModel):
    """Request body for ``POST /v1/assign_codes``."""

    model_config = {
        "json_schema_extra": {"examples": _assign_codes_request_examples()},
    }

    coding_framework: dict[str, Any]
    feedback_items: list[FeedbackItem] = Field(min_length=1)
    max_codes: int = Field(default=1, ge=1, le=50)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class CodeItem(BaseModel):
    """A single code item."""

    code_id: str
    code_label: str


class CodeItems(BaseModel):
    """List of code items assigned to one feedback item."""

    feedback_item_id: str
    code_items: list[CodeItem]


class AssignCodesResponse(BaseModel):
    """Response body for ``POST /v1/assign_codes``."""

    coded_feedback_items: list[CodeItems]


class HealthResponse(BaseModel):
    """Response body for the ``GET /v1/health`` endpoint."""

    status: str = Field(description="Service health status.")
    version: str = Field(description="Package version string.")


class DistributionStatsResponse(BaseModel):
    """Distribution statistics for a metric.

    Attributes
    ----------
    avg : float
        Mean value.
    min : float
        Minimum value.
    max : float
        Maximum value.
    p5 : float
        5th percentile.
    p95 : float
        95th percentile.
    """

    avg: float
    min: float
    max: float
    p5: float
    p95: float


class TokenStatsResponse(DistributionStatsResponse):
    """Token distribution statistics with a total count.

    Attributes
    ----------
    total : int
        Total number of tokens.
    """

    total: int


class OperationStatsResponse(BaseModel):
    """Per-operation aggregated stats."""

    operation: str = Field(description="Orchestrator operation name.")
    total_calls: int
    failed_calls: int
    cost_usd: Decimal = Field(
        description="Sum of cost_usd over status='ok' rows in the window.",
    )
    input_tokens_total: int
    output_tokens_total: int

    @field_serializer("cost_usd")
    def _serialize_cost(self, v: Decimal) -> float:
        return float(v)


class UsageStatsResponse(BaseModel):
    """Aggregated usage statistics for a single tenant or grand total.

    Attributes
    ----------
    tenant_id : str | None
        Tenant identifier, or None for grand total.
    from_ : datetime | None
        Echoed inclusive lower bound of the time filter (or None).
    to : datetime | None
        Echoed exclusive upper bound of the time filter (or None).
    total_calls : int
        Total attempts (successful + failed).
    failed_calls : int
        Attempts with ``status='error'``.
    total_cost_usd : Decimal
        Sum of cost over successful attempts only.
    call_duration : DistributionStatsResponse
        Call duration distribution in milliseconds (successful attempts only).
    input_tokens : TokenStatsResponse
        Input token distribution (successful attempts only).
    output_tokens : TokenStatsResponse
        Output token distribution (successful attempts only).
    by_operation : list[OperationStatsResponse]
        Per-operation breakdown, sorted cost desc with ties by operation asc.
    """

    model_config = {"populate_by_name": True}

    tenant_id: str | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    total_calls: int
    failed_calls: int = 0
    total_cost_usd: Decimal = Decimal("0")
    call_duration: DistributionStatsResponse
    input_tokens: TokenStatsResponse
    output_tokens: TokenStatsResponse
    by_operation: list[OperationStatsResponse] = Field(default_factory=list)

    @field_serializer("total_cost_usd")
    def _serialize_total_cost(self, v: Decimal) -> float:
        return float(v)


class AllUsageStatsResponse(BaseModel):
    """Per-tenant + grand total usage with optional echoed time window."""

    model_config = {"populate_by_name": True}

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    tenants: list[UsageStatsResponse]
    total: UsageStatsResponse


class ErrorFieldDetail(BaseModel):
    """Per-field validation error detail."""

    field: str = Field(description="Field that failed validation.")
    issue: str = Field(description="Description of the validation issue.")


class ErrorDetail(BaseModel):
    """Structured error information."""

    code: str = Field(description="Stable string error code.")
    message: str = Field(description="Human-readable error message.")
    request_id: str = Field(
        description="Unique identifier of the request that caused the error.",
    )
    fields: list[ErrorFieldDetail] | None = Field(
        default=None,
        description="Per-field validation details, present only for 422 responses.",
    )


class ErrorResponse(BaseModel):
    """Envelope for all error responses."""

    error: ErrorDetail = Field(description="Error detail payload.")
