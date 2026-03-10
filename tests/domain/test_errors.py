"""Tests for domain error hierarchy."""

from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    AuthenticationError,
    DocumentsTooLargeError,
    DomainError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)


class TestDomainError:
    def test_is_subclass_of_exception(self):
        assert issubclass(DomainError, Exception)

    def test_carries_message(self):
        err = DomainError("something went wrong")
        assert str(err) == "something went wrong"


class TestAnalysisError:
    def test_is_subclass_of_domain_error(self):
        assert issubclass(AnalysisError, DomainError)

    def test_carries_message(self):
        err = AnalysisError("analysis failed")
        assert str(err) == "analysis failed"


class TestAnalysisTimeoutError:
    def test_is_subclass_of_analysis_error(self):
        assert issubclass(AnalysisTimeoutError, AnalysisError)

    def test_carries_message(self):
        err = AnalysisTimeoutError("deadline exceeded")
        assert str(err) == "deadline exceeded"


class TestDocumentsTooLargeError:
    def test_is_subclass_of_analysis_error(self):
        assert issubclass(DocumentsTooLargeError, AnalysisError)

    def test_exposes_estimated_tokens_and_limit(self):
        err = DocumentsTooLargeError("too large", estimated_tokens=50000, limit=32000)
        assert err.estimated_tokens == 50000
        assert err.limit == 32000

    def test_carries_message(self):
        err = DocumentsTooLargeError("too large", estimated_tokens=50000, limit=32000)
        assert str(err) == "too large"


class TestLLMError:
    def test_is_subclass_of_domain_error(self):
        assert issubclass(LLMError, DomainError)

    def test_carries_message(self):
        err = LLMError("llm failure")
        assert str(err) == "llm failure"


class TestLLMTimeoutError:
    def test_is_subclass_of_llm_error(self):
        assert issubclass(LLMTimeoutError, LLMError)

    def test_carries_message(self):
        err = LLMTimeoutError("timeout")
        assert str(err) == "timeout"


class TestLLMRateLimitError:
    def test_is_subclass_of_llm_error(self):
        assert issubclass(LLMRateLimitError, LLMError)

    def test_carries_message(self):
        err = LLMRateLimitError("rate limited")
        assert str(err) == "rate limited"


class TestAuthenticationError:
    def test_is_subclass_of_domain_error(self):
        assert issubclass(AuthenticationError, DomainError)

    def test_carries_message(self):
        err = AuthenticationError("invalid key")
        assert str(err) == "invalid key"
