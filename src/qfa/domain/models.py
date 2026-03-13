"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class FeedbackDocument(BaseModel):
    """A single feedback document submitted for analysis.

    Attributes
    ----------
    id : str
        Unique identifier for the document.
    text : str
        The feedback text content. Must be between 1 and 100,000 characters.
    metadata : dict[str, str | int | float | bool]
        Optional metadata key-value pairs associated with the document.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AnalysisRequest(BaseModel):
    """A request to analyze one or more feedback documents.

    Attributes
    ----------
    documents : tuple[FeedbackDocument, ...]
        Non-empty tuple of feedback documents to analyze.
    prompt : str
        The analysis prompt. Must be between 1 and 4000 characters.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    documents: tuple[FeedbackDocument, ...] = Field(min_length=1)
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
    name : str
        Human-readable name for the API key.
    key : str
        The API key value.
    tenant_id : str
        The tenant this key belongs to.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    key: SecretStr
    tenant_id: str
