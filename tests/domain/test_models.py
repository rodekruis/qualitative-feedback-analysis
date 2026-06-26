"""Tests for non-usage domain models (requests, responses, API keys, sensitivity)."""

import pytest
from pydantic import ValidationError

from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    LLMResponse,
    SensitivityAnalysisResultModelList,
    TenantApiKey,
)
from qfa.domain.sensitivity_types import SensitivityType

# --- FeedbackRecordModel ---


class TestFeedbackRecordModel:
    def test_construct_with_valid_data(self):
        doc = FeedbackRecordModel(id="doc-1", content="Some feedback")
        assert doc.id == "doc-1"
        assert doc.content == "Some feedback"

    def test_metadata_defaults_to_empty(self):
        doc = FeedbackRecordModel(id="doc-1", content="Some feedback")
        assert isinstance(doc.metadata, FeedbackRecordMetadataModel)
        assert doc.metadata.model_dump(exclude_none=True) == {}

    def test_metadata_with_values(self):
        meta = {"source": "email", "score": 5, "weight": 0.8, "urgent": True}
        doc = FeedbackRecordModel(id="doc-1", content="feedback", metadata=meta)
        assert doc.metadata.model_dump(exclude_none=True) == meta

    def test_frozen_raises_on_assignment(self):
        doc = FeedbackRecordModel(id="doc-1", content="feedback")
        with pytest.raises(ValidationError):
            doc.content = "changed"

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError):
            FeedbackRecordModel(id="doc-1", content="")

    def test_text_exceeding_max_length_raises(self):
        with pytest.raises(ValidationError):
            FeedbackRecordModel(id="doc-1", content="x" * 100_001)

    def test_text_at_max_length_is_valid(self):
        doc = FeedbackRecordModel(id="doc-1", content="x" * 100_000)
        assert len(doc.content) == 100_000


# --- AnalysisRequest ---


class TestAnalysisRequestModel:
    def _make_doc(self, doc_id: str = "doc-1") -> FeedbackRecordModel:
        return FeedbackRecordModel(id=doc_id, content="feedback text")

    def test_construct_with_valid_data(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="Summarize feedback", tenant_id="tenant-1"
        )
        assert req.feedback_records == (doc,)
        assert req.prompt == "Summarize feedback"
        assert req.tenant_id == "tenant-1"

    def test_feedback_records_is_a_tuple(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        assert isinstance(req.feedback_records, tuple)

    def test_frozen_raises_on_assignment(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        with pytest.raises(ValidationError):
            req.prompt = "changed"

    def test_empty_feedback_records_raises(self):
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(), prompt="Summarize", tenant_id="tenant-1"
            )

    def test_empty_prompt_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(doc,), prompt="", tenant_id="tenant-1"
            )

    def test_prompt_exceeding_max_length_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(doc,), prompt="x" * 4001, tenant_id="tenant-1"
            )

    def test_prompt_at_max_length_is_valid(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="x" * 4000, tenant_id="tenant-1"
        )
        assert len(req.prompt) == 4000


# --- AnalysisResult ---


class TestAnalysisResultModel:
    def test_construct_with_valid_data(self):
        result = AnalysisResultModel(
            result="Summary text",
        )
        assert result.result == "Summary text"

    def test_frozen_raises_on_assignment(self):
        result = AnalysisResultModel(
            result="Summary",
        )
        with pytest.raises(ValidationError):
            result.result = "changed"


def test_analysis_request_accepts_hierarchical_mode() -> None:
    """``AnalysisRequestModel.mode`` accepts ``"hierarchical"`` (and defaults to single_pass).

    Why: #124 introduces the second mode; omitting ``mode`` must still
    default to ``single_pass`` so existing callers are unaffected.
    """
    record = FeedbackRecordModel(id="r1", content="x", metadata={})
    default = AnalysisRequestModel(
        feedback_records=(record,), prompt="p", tenant_id="t"
    )
    assert default.mode == "single_pass"
    hierarchical = AnalysisRequestModel(
        feedback_records=(record,), prompt="p", tenant_id="t", mode="hierarchical"
    )
    assert hierarchical.mode == "hierarchical"


def test_analysis_result_carries_optional_hierarchical_fields() -> None:
    """``AnalysisResultModel`` exposes optional confidence and coding_trends fields.

    Why: the hierarchical path reports a coverage-weighted ``confidence``
    and the trend table; single_pass leaves them None/default so the
    response is unchanged.
    """
    from qfa.domain.clustering_models import CodingTrendTable

    result = AnalysisResultModel(
        result="text",
        confidence=0.83,
        coding_trends=CodingTrendTable(periods=(), cells=()),
    )
    assert result.confidence == 0.83
    assert result.coding_trends is not None
    # Defaults for the single_pass path:
    default = AnalysisResultModel(result="text")
    assert default.confidence is None
    assert default.coding_trends is None


# --- LLMResponse ---


class TestLLMResponse:
    def test_construct_with_valid_data(self):
        resp = LLMResponse(
            structured="Generated text",
            model="gpt-4",
            prompt_tokens=80,
            completion_tokens=40,
            cost=0.001,
        )
        assert resp.structured == "Generated text"
        assert resp.model == "gpt-4"
        assert resp.prompt_tokens == 80
        assert resp.completion_tokens == 40

    def test_frozen_raises_on_assignment(self):
        resp = LLMResponse(
            structured="text",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.001,
        )
        with pytest.raises(ValidationError):
            resp.structured = "changed"


# --- TenantApiKey ---


class TestTenantApiKey:
    def test_construct_with_plain_key_hashes_and_discards_key(self):
        key = TenantApiKey(
            key_id="tenant-1-0",
            name="prod-key",
            key="sk-abc123",  # type:ignore [ty:invalid-argument-type]
            hashed_key=None,  # type:ignore [ty:invalid-argument-type]
            tenant_id="tenant-1",
        )
        assert key.key_id == "tenant-1-0"
        assert key.name == "prod-key"
        assert key.key is None
        assert key.hashed_key.get_secret_value() == TenantApiKey.hash_key("sk-abc123")
        assert key.tenant_id == "tenant-1"

    def test_construct_with_hashed_key(self):
        key_hash = TenantApiKey.hash_key("sk-abc123")
        key = TenantApiKey(
            key_id="tenant-1-0",
            name="prod-key",
            hashed_key=key_hash,  # type:ignore [ty:invalid-argument-type]
            tenant_id="tenant-1",
        )
        assert key.key is None
        assert key.hashed_key.get_secret_value() == key_hash

    def test_rejects_mismatched_key_and_hashed_key(self):
        with pytest.raises(ValidationError):
            TenantApiKey(
                key_id="tenant-1-0",
                name="prod-key",
                key="sk-abc123",  # type:ignore [ty:invalid-argument-type]
                hashed_key="not-the-right-hash",  # type:ignore [ty:invalid-argument-type]
                tenant_id="tenant-1",
            )

    def test_frozen_raises_on_assignment(self):
        key = TenantApiKey(
            key_id="tenant-1-0",
            name="prod-key",
            key="sk-abc123",  # type:ignore [ty:invalid-argument-type]
            hashed_key=None,  # type:ignore [ty:invalid-argument-type]
            tenant_id="tenant-1",
        )
        with pytest.raises(ValidationError):
            key.name = "changed"


class TestSensitivityModels:
    def test_sensitive_result_parses_enum_codes_from_json(self):
        result = SensitivityAnalysisResultModelList.model_validate_json(
            """
            {
                "results": [
                    {
                        "feedback_record_id": "doc-1",
                        "sensitivity_types": ["CORRUPTION"],
                        "explanation": "Contains a bribery allegation."
                    }
                ]
            }
            """
        )

        assert result.results[0].sensitivity_types == (SensitivityType.CORRUPTION,)
        assert result.results[0].explanation == "Contains a bribery allegation."

    def test_sensitivity_enum_uses_short_stable_values(self):
        assert SensitivityType.CORRUPTION.value == "CORRUPTION"


class TestAnalysisRequestMode:
    def test_default_mode_is_single_pass(self):
        """``mode`` defaults to ``single_pass`` when omitted by callers."""
        req = AnalysisRequestModel(
            feedback_records=(FeedbackRecordModel(id="d", content="t"),),
            prompt="x",
            tenant_id="t",
        )
        assert req.mode == "single_pass"

    def test_explicit_single_pass_accepted(self):
        """``mode=single_pass`` is the documented explicit value."""
        req = AnalysisRequestModel(
            feedback_records=(FeedbackRecordModel(id="d", content="t"),),
            prompt="x",
            tenant_id="t",
            mode="single_pass",
        )
        assert req.mode == "single_pass"

    def test_invalid_mode_rejected(self):
        """An unknown ``mode`` value raises a validation error.

        Why: the Literal type must enforce the allowlist so callers cannot
        pass arbitrary strings; guards against future misrouting.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(FeedbackRecordModel(id="d", content="t"),),
                prompt="x",
                tenant_id="t",
                mode="batch",  # ty: ignore[invalid-argument-type]  # intentionally invalid
            )

    def test_hierarchical_mode_accepted(self):
        """``mode=hierarchical`` is accepted after #124 widens the Literal.

        Why: ensures the widening did not accidentally break the Literal
        constraint for the new valid value.
        """
        req = AnalysisRequestModel(
            feedback_records=(FeedbackRecordModel(id="d", content="t"),),
            prompt="x",
            tenant_id="t",
            mode="hierarchical",
        )
        assert req.mode == "hierarchical"


class TestAnalysisResultModelExtended:
    def test_quality_score_can_be_none(self):
        """``quality_score=None`` represents judge unavailability."""
        m = AnalysisResultModel(
            result="ok", quality_score=None, uncertainty_explanation="why"
        )
        assert m.quality_score is None

    def test_quality_score_float_accepted(self):
        """Floats in ``[0, 1]`` are accepted."""
        m = AnalysisResultModel(
            result="ok", quality_score=0.42, uncertainty_explanation="why"
        )
        assert m.quality_score == 0.42
