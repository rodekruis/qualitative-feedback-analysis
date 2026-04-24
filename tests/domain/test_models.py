"""Tests for domain models."""

import pytest
from pydantic import ValidationError

from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    FeedbackItemModel,
    LLMResponse,
    TenantApiKey,
)

# --- FeedbackItemModel ---


class TestFeedbackItemModel:
    def test_construct_with_valid_data(self):
        doc = FeedbackItemModel(id="doc-1", text="Some feedback")
        assert doc.id == "doc-1"
        assert doc.text == "Some feedback"

    def test_metadata_defaults_to_empty_dict(self):
        doc = FeedbackItemModel(id="doc-1", text="Some feedback")
        assert doc.metadata == {}

    def test_metadata_with_values(self):
        meta = {"source": "email", "score": 5, "weight": 0.8, "urgent": True}
        doc = FeedbackItemModel(id="doc-1", text="feedback", metadata=meta)
        assert doc.metadata == meta

    def test_frozen_raises_on_assignment(self):
        doc = FeedbackItemModel(id="doc-1", text="feedback")
        with pytest.raises(ValidationError):
            doc.text = "changed"

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError):
            FeedbackItemModel(id="doc-1", text="")

    def test_text_exceeding_max_length_raises(self):
        with pytest.raises(ValidationError):
            FeedbackItemModel(id="doc-1", text="x" * 100_001)

    def test_text_at_max_length_is_valid(self):
        doc = FeedbackItemModel(id="doc-1", text="x" * 100_000)
        assert len(doc.text) == 100_000


# --- AnalysisRequest ---


class TestAnalysisRequestModel:
    def _make_doc(self, doc_id: str = "doc-1") -> FeedbackItemModel:
        return FeedbackItemModel(id=doc_id, text="feedback text")

    def test_construct_with_valid_data(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="Summarize feedback", tenant_id="tenant-1"
        )
        assert req.documents == (doc,)
        assert req.prompt == "Summarize feedback"
        assert req.tenant_id == "tenant-1"

    def test_documents_is_a_tuple(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        assert isinstance(req.documents, tuple)

    def test_frozen_raises_on_assignment(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        with pytest.raises(ValidationError):
            req.prompt = "changed"

    def test_empty_documents_raises(self):
        with pytest.raises(ValidationError):
            AnalysisRequestModel(documents=(), prompt="Summarize", tenant_id="tenant-1")

    def test_empty_prompt_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(documents=(doc,), prompt="", tenant_id="tenant-1")

    def test_prompt_exceeding_max_length_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                documents=(doc,), prompt="x" * 4001, tenant_id="tenant-1"
            )

    def test_prompt_at_max_length_is_valid(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="x" * 4000, tenant_id="tenant-1"
        )
        assert len(req.prompt) == 4000


# --- AnalysisResult ---


class TestAnalysisResultModel:
    def test_construct_with_valid_data(self):
        result = AnalysisResultModel(
            result="Summary text",
            model="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            cost=0.001,
        )
        assert result.result == "Summary text"
        assert result.model == "gpt-4"
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50

    def test_frozen_raises_on_assignment(self):
        result = AnalysisResultModel(
            result="Summary",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.001,
        )
        with pytest.raises(ValidationError):
            result.result = "changed"


# --- LLMResponse ---


class TestLLMResponse:
    def test_construct_with_valid_data(self):
        resp = LLMResponse(
            text="Generated text",
            model="gpt-4",
            prompt_tokens=80,
            completion_tokens=40,
            cost=0.001,
        )
        assert resp.text == "Generated text"
        assert resp.model == "gpt-4"
        assert resp.prompt_tokens == 80
        assert resp.completion_tokens == 40

    def test_frozen_raises_on_assignment(self):
        resp = LLMResponse(
            text="text",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.001,
        )
        with pytest.raises(ValidationError):
            resp.text = "changed"


# --- TenantApiKey ---


class TestTenantApiKey:
    def test_construct_with_valid_data(self):
        key = TenantApiKey(
            key_id="tenant-1-0", name="prod-key", key="sk-abc123", tenant_id="tenant-1"
        )
        assert key.key_id == "tenant-1-0"
        assert key.name == "prod-key"
        assert key.key.get_secret_value() == "sk-abc123"
        assert key.tenant_id == "tenant-1"

    def test_frozen_raises_on_assignment(self):
        key = TenantApiKey(
            key_id="tenant-1-0", name="prod-key", key="sk-abc123", tenant_id="tenant-1"
        )
        with pytest.raises(ValidationError):
            key.name = "changed"
