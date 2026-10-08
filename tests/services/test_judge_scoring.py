"""Tests for the shared ``SCORE:``/``EXPLANATION:`` judge-reply parser."""

import pytest

from qfa.domain.errors import AnalysisError
from qfa.services.judge_scoring import parse_judge_response


class TestParseJudgeResponse:
    """Unit coverage for the SCORE:/EXPLANATION: free-text parser itself."""

    def test_parses_well_formed_reply(self):
        judged = parse_judge_response("SCORE: 0.72\nEXPLANATION: Clear fit.")
        assert judged.score == 0.72
        assert judged.explanation == "Clear fit."

    def test_tolerates_case_and_surrounding_whitespace(self):
        judged = parse_judge_response("  score:  0.5  \n  explanation:  Plausible.  ")
        assert judged.score == 0.5
        assert judged.explanation == "Plausible."

    def test_multiline_explanation_is_kept_in_full(self):
        judged = parse_judge_response(
            "SCORE: 0.4\nEXPLANATION: Weak on the first point.\nAlso weak on the second."
        )
        assert judged.explanation == (
            "Weak on the first point.\nAlso weak on the second."
        )

    def test_missing_score_line_raises_analysis_error(self):
        with pytest.raises(AnalysisError, match="unparsable response"):
            parse_judge_response("EXPLANATION: No score given.")

    def test_non_numeric_score_raises_analysis_error(self):
        with pytest.raises(AnalysisError, match="unparsable response"):
            parse_judge_response("SCORE: high\nEXPLANATION: Confident.")
