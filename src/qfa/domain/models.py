"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class FeedbackItem(BaseModel):
    """A single feedback item submitted for analysis.

    Attributes
    ----------
    id : str
        Unique identifier for the feedback item.
    text : str
        The feedback text content. Must be between 1 and 100,000 characters.
    metadata : dict[str, str | int | float | bool]
        Optional metadata key-value pairs associated with the feedback item.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AnalysisRequest(BaseModel):
    """A request to analyze one or more feedback items.

    Attributes
    ----------
    documents : tuple[FeedbackItem, ...]
        Non-empty tuple of feedback items to analyze.
    prompt : str
        The analysis prompt. Must be between 1 and 4000 characters.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    documents: tuple[FeedbackItem, ...] = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=4000)
    tenant_id: str


class AnalysisResult(BaseModel):
    """The result of a feedback analysis.

    Attributes
    ----------
    result : str
        The analysis output text.
    model : str
        The LLM model used for analysis.
    prompt_tokens : int
        Number of tokens in the prompt.
    completion_tokens : int
        Number of tokens in the completion.
    """

    model_config = ConfigDict(frozen=True)

    result: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class SummaryRequest(BaseModel):
    """A request to summarize one or more feedback items individually.

    Attributes
    ----------
    feedback_items : tuple[FeedbackItem, ...]
        Non-empty tuple of feedback items to summarize.
    output_language : str | None
        Optional target language for all summaries.
    prompt : str | None
        Optional extra instruction appended to the default summarize prompt.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItem, ...] = Field(min_length=1)
    output_language: str | None = None
    prompt: str | None = Field(default=None, max_length=4000)
    tenant_id: str


class FeedbackItemSummary(BaseModel):
    """Summary output for a single feedback item.

    Attributes
    ----------
    id : str
        Identifier of the source feedback item.
    title : str
        Generated short title for the feedback item.
    summary : str
        Generated bullet-point summary for the feedback item.
    quality_score : float
        Judge model score for summary quality (faithfulness, coverage, clarity)
        in the range 0.0-1.0.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    summary: str
    quality_score: float = Field(ge=0.0, le=1.0)


class SummaryResult(BaseModel):
    """The result of summarizing multiple feedback items individually.

    Attributes
    ----------
    feedback_item_summaries : tuple[FeedbackItemSummary, ...]
        Per-feedback-item summaries returned by the summarize flow.
    """

    model_config = ConfigDict(frozen=True)

    feedback_item_summaries: tuple[FeedbackItemSummary, ...]


class CodingAssignmentRequest(BaseModel):
    """A request to assign hierarchical codes to feedback items.

    Attributes
    ----------
    feedback_items : tuple[FeedbackItem, ...]
        Non-empty tuple of feedback items to code (``text`` is the body to classify).
    coding_framework : dict[str, Any]
        Hierarchical framework payload (e.g. API ``coding_framework``), including
        ``coding_frames`` and nested ``types``, ``categories``, and ``codes``.
    max_codes : int
        Maximum number of leaf codes to retain per feedback item.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItem, ...] = Field(min_length=1)
    coding_framework: dict[str, Any]
    max_codes: int = Field(ge=1, le=50)
    tenant_id: str


class AssignedCode(BaseModel):
    """A single leaf code assigned to a feedback item.

    Attributes
    ----------
    code_id : str
        Stable identifier from the framework (e.g. slug path).
    code_label : str
        Human-readable code name.
    """

    model_config = ConfigDict(frozen=True)

    code_id: str
    code_label: str


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
    """Raw response from an LLM provider.

    Attributes
    ----------
    text : str
        The generated text.
    model : str
        The model that produced the response.
    prompt_tokens : int
        Number of tokens in the prompt.
    completion_tokens : int
        Number of tokens in the completion.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class TenantApiKey(BaseModel):
    """An API key associated with a tenant.

    Attributes
    ----------
    key_id : str
        Unique identifier for the key (e.g. ``"tenant-0"``).
    name : str
        Human-readable name for the API key.
    key : SecretStr
        The API key value.
    tenant_id : str
        The tenant this key belongs to.
    """

    model_config = ConfigDict(frozen=True)

    key_id: str
    name: str
    key: SecretStr
    tenant_id: str
