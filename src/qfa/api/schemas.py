"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _assign_codes_request_examples() -> list[dict[str, Any]]:
    """Build Swagger ``examples`` from ``fixtures/coding_framework.json`` + COVID-19 codebook quotes."""
    root = Path(__file__).resolve().parents[3]
    path = root / "fixtures" / "coding_framework.json"
    if not path.is_file():
        return [
            {
                "coding_framework": {"coding_frames": []},
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
    # Verbatim long examples from the COVID-19 frame in the coding framework (Excel export).
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
    """A single feedback item in an analysis request.

    Attributes
    ----------
    id : str
        Unique identifier for the feedback item.
    text : str
        The feedback text content. Must be between 1 and 100,000 characters.
    metadata : dict[str, str | int | float | bool]
        Optional metadata key-value pairs associated with the feedback item.
    """

    id: str
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    """Request body for the ``POST /v1/analyze`` endpoint.

    Attributes
    ----------
    documents : list[FeedbackItemInput]
        Non-empty list of feedback items to analyze.
    prompt : str
        The analysis prompt. Must be between 1 and 4,000 characters.
    """

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

    documents: list[FeedbackItemInput] = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=4_000)


class AnalyzeResponse(BaseModel):
    """Response body for the ``POST /v1/analyze`` endpoint.

    Attributes
    ----------
    analysis : str
        The analysis output text.
    document_count : int
        Number of documents that were analyzed.
    request_id : str
        Unique identifier for this request.
    """

    analysis: str
    document_count: int
    request_id: str


class SummarizeFeedbackMetadata(BaseModel):
    """Metadata for a feedback item in a summarize request."""

    created: datetime
    feedback_item_id: str
    coding_level_1: str
    coding_level_2: str
    coding_level_3: str


class SummarizeFeedbackItem(BaseModel):
    """A single feedback item for ``POST /v1/summarize``."""

    id: str
    content: str = Field(min_length=1, max_length=100_000)
    metadata: SummarizeFeedbackMetadata


class SummarizeRequest(BaseModel):
    """Request body for the ``POST /v1/summarize`` endpoint.

    Attributes
    ----------
    feedback_items : list[SummarizeFeedbackItem]
        Non-empty list of feedback items to summarize individually.
    output_language : str | None
        Optional target language for summaries and titles for every item.
    prompt : str | None
        Optional extra instruction appended to the default summarize prompt.
    """

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

    feedback_items: list[SummarizeFeedbackItem] = Field(min_length=1)
    output_language: str | None = None
    prompt: str | None = Field(default=None, max_length=4_000)


class FeedbackItemSummary(BaseModel):
    """Summary response item for a single feedback item.

    Attributes
    ----------
    id : str
        Identifier of the source feedback item.
    title : str
        Generated short title for the feedback item.
    summary : str
        Generated bullet-point summary for the feedback item.
    quality_score : float
        Judge score for the summary text only, in the range 0.0-1.0.
    """

    id: str
    title: str
    summary: str
    quality_score: float = Field(ge=0.0, le=1.0)


class SummarizeResponse(BaseModel):
    """Response body for the ``POST /v1/summarize`` endpoint.

    Attributes
    ----------
    summaries : list[FeedbackItemSummary]
        Title and summary for each submitted feedback item.
    """

    summaries: list[FeedbackItemSummary]


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
    explanation: str


class CodeItems(BaseModel):
    """List of code items assigned to one feedback item."""

    feedback_item_id: str
    code_items: list[CodeItem]


class AssignCodesResponse(BaseModel):
    """Response body for ``POST /v1/assign_codes``."""

    coded_feedback_items: list[CodeItems]


class HealthResponse(BaseModel):
    """Response body for the ``GET /v1/health`` endpoint.

    Attributes
    ----------
    status : str
        Service health status.
    version : str
        Package version string.
    """

    status: str
    version: str


class ErrorFieldDetail(BaseModel):
    """Per-field validation error detail.

    Attributes
    ----------
    field : str
        The field that failed validation.
    issue : str
        Description of the validation issue.
    """

    field: str
    issue: str


class ErrorDetail(BaseModel):
    """Structured error information.

    Attributes
    ----------
    code : str
        Stable string error code.
    message : str
        Human-readable error message.
    request_id : str
        Unique identifier of the request that caused the error.
    fields : list[ErrorFieldDetail] | None
        Per-field validation details, present only for 422 responses.
    """

    code: str
    message: str
    request_id: str
    fields: list[ErrorFieldDetail] | None = None


class ErrorResponse(BaseModel):
    """Envelope for all error responses.

    Attributes
    ----------
    error : ErrorDetail
        The error detail payload.
    """

    error: ErrorDetail
