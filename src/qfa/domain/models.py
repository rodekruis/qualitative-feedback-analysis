"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

import hashlib
import secrets
from typing import Any, Generic, TypeVar, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

from qfa.domain.sensitivity_types import SensitivityType


class FeedbackRecordModel(BaseModel):
    """A single feedback record submitted for analysis."""

    model_config = ConfigDict(frozen=True)

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


class AnalysisRequestModel(BaseModel):
    """A request to analyze one or more feedback records."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to analyze.",
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


class SummaryRequestModel(BaseModel):
    """A request to summarize one or more feedback records individually."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to summarize.",
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


class FeedbackRecordSummaryModel(BaseModel):
    """Summary output for a single feedback record."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Identifier of the source feedback record.")
    title: str = Field(description="Generated short title for the feedback record.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback record."
    )
    quality_score: float = Field(  # TODO implement actual llm-as-a-judge for this field
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )


class SummaryResultModel(BaseModel):
    """The result of summarizing multiple feedback records individually."""

    model_config = ConfigDict(frozen=True)

    feedback_record_summaries: tuple[FeedbackRecordSummaryModel, ...] = Field(
        description="Per-feedback-record summaries returned by the summarize flow.",
    )


class AggregateSummaryResultModel(BaseModel):
    """The result of summarizing multiple feedback records as a single aggregate.

    # TODO come up with nice solution for non-mutable quality-score, so this can be a frozen class.
    """

    ids: tuple[str, ...] = Field(
        description="Identifiers of all source feedback records."
    )
    title: str = Field(description="Generated short title for the aggregate summary.")
    summary: str = Field(
        description="Generated bullet-point summary ordered by theme frequency."
    )
    quality_score: float = Field(
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )


class CodingAssignmentRequestModel(BaseModel):
    """A request to assign hierarchical codes to feedback records."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to code.",
    )
    coding_framework: dict[str, Any] = Field(
        description="Hierarchical coding framework with types, categories, and codes.",
    )
    max_codes: int = Field(
        ge=1,
        le=50,
        description="Maximum number of leaf codes to retain per feedback record.",
    )
    confidence_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Minimum confidence required at each hierarchy level to retain an assignment.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AssignedCodeModel(BaseModel):
    """A single leaf code assigned to a feedback record."""

    model_config = ConfigDict(frozen=True)

    code_id: str = Field(description="Stable identifier from the coding framework.")
    code_label: str = Field(description="Human-readable code name.")
    confidence_type: float = Field(
        description="Judge confidence that the Type level fits the feedback record (0-1)."
    )
    confidence_category: float = Field(
        description="Judge confidence that the Category level fits the feedback record (0-1)."
    )
    confidence_code: float = Field(
        description="Judge confidence that the Code level fits the feedback record (0-1)."
    )
    confidence_aggregate: float = Field(
        description="Overall confidence, computed as min of the three level confidences."
    )
    explanation: str = Field(
        description="Judge explanation combining scores from all three hierarchy levels."
    )


class CodedFeedbackRecordModel(BaseModel):
    """Coding output for one feedback record."""

    model_config = ConfigDict(frozen=True)

    feedback_record_id: str = Field(
        description="Identifier of the source feedback record.",
    )
    assigned_codes: tuple[AssignedCodeModel, ...] = Field(
        description="Leaf codes selected for this feedback record.",
    )


class CodingAssignmentResultModel(BaseModel):
    """The result of assigning codes to multiple feedback records."""

    model_config = ConfigDict(frozen=True)

    coded_feedback_records: tuple[CodedFeedbackRecordModel, ...] = Field(
        description="Per-feedback-record coding results aligned with the request order.",
    )


class SensitivityAnalysisRequestModel(BaseModel):
    """A request to analyze feedback records for sensitivity."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to analyze for sensitivity.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class SensitivityAnalysisResultModel(BaseModel):
    """The result of analyzing feedback records for sensitivity."""

    model_config = ConfigDict(frozen=True)

    feedback_record_id: str = Field(
        description="Identifier of the source feedback record.",
    )
    sensitivity_types: tuple[SensitivityType, ...] = Field(
        description="Sensitivity types identified in the feedback record.",
    )
    explanation: str = Field(
        description="Natural-language explanation for why the record was classified this way."
    )

    @property
    def is_sensitive(self) -> bool:
        """Convenience property indicating whether any sensitivity types were detected."""
        return len(self.sensitivity_types) > 0


class SensitivityAnalysisResultModelList(BaseModel):
    """The result of analyzing feedback records for sensitivity."""

    model_config = ConfigDict(frozen=True)

    results: tuple[SensitivityAnalysisResultModel, ...] = Field(
        description="Sensitivity analysis results for each feedback record.",
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
    key: SecretStr | None = Field(
        default=None,
        description="Plain API key accepted at construction time and discarded after hashing.",
        exclude=True,
        repr=False,
    )
    hashed_key: SecretStr = Field(
        description="scrypt-derived hash of the API key value."
    )
    tenant_id: str = Field(description="Tenant identifier this key belongs to.")
    is_superuser: bool = False

    @staticmethod
    def hash_key(key: str) -> str:
        """Return a stable scrypt-derived hex digest for an API key."""
        return hashlib.scrypt(
            key.encode("utf-8"),
            salt=b"",
            n=2**14,
            r=8,
            p=1,
        ).hex()

    @model_validator(mode="before")
    @classmethod
    def _normalize_key_inputs(cls, data: Any) -> Any:
        """Normalize input to accept either 'key' or 'hashed_key' but not both, and compute the hash if only 'key' is provided.

        This allows flexible construction while ensuring that the model instance only retains the hashed key for security.
        """
        if not isinstance(data, dict):
            return data

        raw_key = data.get("key")
        raw_hashed = data.get("hashed_key")
        has_key = raw_key is not None
        has_hashed = raw_hashed is not None

        if not has_key and not has_hashed:
            raise ValueError("Either 'key' or 'hashed_key' must be provided")

        if has_key and has_hashed:
            raise ValueError(
                "Only one of 'key' or 'hashed_key' should be provided, not both"
            )

        if has_key:
            if isinstance(raw_key, SecretStr):
                normalized_key = raw_key.get_secret_value()
            else:
                normalized_key = raw_key
            computed_hash = cls.hash_key(normalized_key)

            data["hashed_key"] = computed_hash
            # Ensure plaintext keys are not retained on the model instance.
            data["key"] = None

        return data

    def matches_key(self, provided_key: str) -> bool:
        """Check whether *provided_key* matches this stored API key hash."""
        return secrets.compare_digest(
            self.hashed_key.get_secret_value(),
            self.hash_key(provided_key),
        )


class KeyCreationResponse(BaseModel):
    """Response model for API key creation."""

    key_id: str
    api_key: str


class AuthKeyInfo(BaseModel):
    """Metadata for an API key returned by the auth orchestrator."""

    key_id: str
    name: str
    tenant_id: str
    is_superuser: bool


class TenantInfo(BaseModel):
    """Tenant information returned by the auth orchestrator."""

    tenant_id: str
    name: str
    allows_superusers: bool
