"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class FeedbackItem(BaseModel):
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


class AnalysisRequest(BaseModel):
    """A request to analyze one or more feedback items."""

    model_config = ConfigDict(frozen=True)

    documents: tuple[FeedbackItem, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback items to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4000,
        description="Analysis instruction for the model.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AnalysisResult(BaseModel):
    """The result of a feedback analysis."""

    model_config = ConfigDict(frozen=True)

    result: str = Field(description="Analysis output text.")
    model: str = Field(description="LLM model used for analysis.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(description="Number of tokens in the completion.")
    cost: float = Field(description="Estimated analysis cost in USD.")


class SummaryRequest(BaseModel):
    """A request to summarize one or more feedback items individually."""

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItem, ...] = Field(
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


class FeedbackItemSummary(BaseModel):
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


class SummaryResult(BaseModel):
    """The result of summarizing multiple feedback items individually."""

    model_config = ConfigDict(frozen=True)

    feedback_item_summaries: tuple[FeedbackItemSummary, ...] = Field(
        description="Per-feedback-item summaries returned by the summarize flow.",
    )
    cost: float = Field(description="Estimated summarization cost in USD.")


class AggregateSummaryResult(BaseModel):
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


class CodingAssignmentRequest(BaseModel):
    """A request to assign hierarchical codes to feedback items.

    Attributes
    ----------
    feedback_items : tuple[FeedbackItem, ...]
        Non-empty tuple of feedback items to code (``text`` is the body to classify).
    coding_framework : dict[str, Any]
        Hierarchical framework payload with top-level ``types`` and nested
        ``categories`` and ``codes``.
    max_codes : int
        Maximum number of leaf codes to retain per feedback item.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItem, ...] = Field(min_length=1)
    coding_framework: dict[str, Any]
    max_codes: int = Field(ge=1, le=50)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    tenant_id: str


class AssignedCode(BaseModel):
    """A single leaf code assigned to a feedback item.

    Attributes
    ----------
    code_id : str
        Stable identifier from the framework (e.g. slug path).
    code_label : str
        Human-readable code name.
    confidence_type : float | None
        Judge confidence that the Type level fits the feedback item (0-1).
    confidence_category : float | None
        Judge confidence that the Category level fits the feedback item (0-1).
    confidence_code : float | None
        Judge confidence that the Code level fits the feedback item (0-1).
    confidence_aggregate : float | None
        Overall confidence, computed as min of the three level confidences.
    explanation : str | None
        Judge explanation for the code-level assignment.
    """

    model_config = ConfigDict(frozen=True)

    code_id: str
    code_label: str
    confidence_type: float | None = None
    confidence_category: float | None = None
    confidence_code: float | None = None
    confidence_aggregate: float | None = None
    explanation: str | None = None


class CodedFeedbackItem(BaseModel):
    """Coding output for one feedback item.

    Attributes
    ----------
    feedback_item_id : str
        Identifier of the source feedback item.
    assigned_codes : tuple[AssignedCode, ...]
        Leaf codes selected for this item.
    """

    model_config = ConfigDict(frozen=True)

    feedback_item_id: str
    assigned_codes: tuple[AssignedCode, ...]


class CodingAssignmentResult(BaseModel):
    """The result of assigning codes to multiple feedback items.

    Attributes
    ----------
    coded_feedback_items : tuple[CodedFeedbackItem, ...]
        Per-item coding results, aligned with the request order.
    """

    model_config = ConfigDict(frozen=True)

    coded_feedback_items: tuple[CodedFeedbackItem, ...]


class LLMResponse(BaseModel):
    """Raw response from an LLM provider."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="Generated text.")
    model: str = Field(description="Model that produced the response.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(description="Number of tokens in the completion.")
    cost: float = Field(description="Estimated request cost in USD.")


class TenantApiKey(BaseModel):
    """An API key associated with a tenant."""

    model_config = ConfigDict(frozen=True)

    key_id: str = Field(description="Unique identifier for the API key.")
    name: str = Field(description="Human-readable name for the API key.")
    key: SecretStr = Field(description="Secret API key value.")
    tenant_id: str = Field(description="Tenant identifier this key belongs to.")
