"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from typing import Any, Generic, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class FeedbackItemModel(BaseModel):
    """A single feedback item submitted for analysis."""

    model_config = ConfigDict(frozen=True)

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


class AnalysisRequestModel(BaseModel):
    """A request to analyze one or more feedback items."""

    model_config = ConfigDict(frozen=True)

    documents: tuple[FeedbackItemModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback items to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4000,
        description="Analysis instruction for the model.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AnalysisResultModel(BaseModel):
    """The result of a feedback analysis."""

    model_config = ConfigDict(frozen=True)

    result: str = Field(description="Analysis output text.")
    model: str = Field(description="LLM model used for analysis.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(description="Number of tokens in the completion.")
    cost: float = Field(description="Estimated analysis cost in USD.")


class SummaryRequestModel(BaseModel):
    """A request to summarize one or more feedback items individually."""

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItemModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback items to summarize.",
    )
    output_language: str | None = Field(
        default=None,
        description="Optional target language for all summaries.",
    )
    prompt: str | None = Field(
        default=None,
        max_length=4000,
        description="Optional extra instruction appended to the default summarize prompt.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class FeedbackItemSummaryModel(BaseModel):
    """Summary output for a single feedback item."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Identifier of the source feedback item.")
    title: str = Field(description="Generated short title for the feedback item.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback item."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )


class SummaryResultModel(BaseModel):
    """The result of summarizing multiple feedback items individually."""

    model_config = ConfigDict(frozen=True)

    feedback_item_summaries: tuple[FeedbackItemSummaryModel, ...] = Field(
        description="Per-feedback-item summaries returned by the summarize flow.",
    )
    cost: float = Field(description="Estimated summarization cost in USD.")


class AggregateSummaryResultModel(BaseModel):
    """The result of summarizing multiple feedback items as a single aggregate."""

    model_config = ConfigDict(frozen=True)

    ids: tuple[str, ...] = Field(
        description="Identifiers of all source feedback items."
    )
    title: str = Field(description="Generated short title for the aggregate summary.")
    summary: str = Field(
        description="Generated bullet-point summary ordered by theme frequency."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )
    cost: float = Field(description="Estimated summarization cost in USD.")


class CodingAssignmentRequestModel(BaseModel):
    """A request to assign hierarchical codes to feedback items."""

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItemModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback items to code.",
    )
    coding_framework: dict[str, Any] = Field(
        description="Hierarchical coding framework with types, categories, and codes.",
    )
    max_codes: int = Field(
        ge=1,
        le=50,
        description="Maximum number of leaf codes to retain per feedback item.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AssignedCodeModel(BaseModel):
    """A single leaf code assigned to a feedback item."""

    model_config = ConfigDict(frozen=True)

    code_id: str = Field(
        description="Stable identifier from the coding framework.",
    )
    code_label: str = Field(description="Human-readable code name.")


class CodedFeedbackItemModel(BaseModel):
    """Coding output for one feedback item."""

    model_config = ConfigDict(frozen=True)

    feedback_item_id: str = Field(
        description="Identifier of the source feedback item.",
    )
    assigned_codes: tuple[AssignedCodeModel, ...] = Field(
        description="Leaf codes selected for this feedback item.",
    )


class CodingAssignmentResultModel(BaseModel):
    """The result of assigning codes to multiple feedback items."""

    model_config = ConfigDict(frozen=True)

    coded_feedback_items: tuple[CodedFeedbackItemModel, ...] = Field(
        description="Per-item coding results aligned with the request order.",
    )


# Define a TypeVar that must be a Pydantic BaseModel
T_Response = TypeVar("T_Response", bound=Union[BaseModel, str])


class LLMResponse(BaseModel, Generic[T_Response]):
    """Raw response from an LLM provider."""

    model_config = ConfigDict(frozen=True)

    structured: T_Response = Field(
        description="Parsed response conforming to the expected schema, either a string or Pydantic model.",
    )
    model: str = Field(description="LLM model that produced the response.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(
        description="Number of tokens in the completion.",
    )
    cost: float = Field(description="Estimated request cost in USD.")


class TenantApiKey(BaseModel):
    """An API key associated with a tenant."""

    model_config = ConfigDict(frozen=True)

    key_id: str = Field(description="Unique identifier for the API key.")
    name: str = Field(description="Human-readable name for the API key.")
    key: SecretStr = Field(description="Secret API key value.")
    tenant_id: str = Field(description="Tenant identifier this key belongs to.")
