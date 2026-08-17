"""Tests for :class:`~qfa.services.coding.CodingService`.

Why its own module: the assign-codes use case is now its own application
service (ADR-017, issue #265), and these tests moved with it from
``test_orchestrator``. Their content is unchanged — the one-shot pick
behaviour, the below-threshold formatter assertions (including the exact
rendered message pinned by #256), and the three confidence-threshold
behaviours still assert the same output.

Per ADR-017 the service under test is constructed over the **real**
:class:`~qfa.services.llm_call_executor.LLMCallExecutor`, itself built over
the existing ``FakeLLMPort`` / ``FakeAnonymizer`` doubles — there is no fake
executor and no fake service.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from qfa.domain.errors import AnalysisError, LLMResponseParseError
from qfa.domain.models import (
    CodingAssignmentRequestModel,
    CodingFramework,
    CodingNode,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    LLMResponse,
)
from qfa.services.coding import (
    NO_CODING_NOTHING_RELEVANT_EXPLANATION,
    CodingService,
    _combine_rejected_explanations,
    _ScoredCode,
)
from qfa.services.coding_classifier import CodingResponse, JudgeResponse
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.settings import OrchestratorSettings

# Reuse the doubles the summarize suite already ships rather than growing a
# second, drifting pair (ADR-017: service tests use the real executor over the
# existing fake driven adapters).
from .test_summarize import FakeAnonymizer, FakeLLMPort

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0
MAX_TOKENS = 10_000


@pytest.fixture
def settings():
    return OrchestratorSettings()


def _make_feedback_record(
    doc_id="doc-1", content="Some feedback text.", metadata=None, url_id=""
):
    return FeedbackRecordModel(
        id=doc_id,
        content=content,
        metadata=FeedbackRecordMetadataModel.model_validate(metadata or {}),
        url_id=url_id,
    )


def _make_llm_response(structured, model="gpt-4", cost=0.001):
    """Wrap one pick or judge payload as the LLM response the fake serves."""
    return LLMResponse(
        structured=structured,
        model=model,
        prompt_tokens=100,
        completion_tokens=50,
        cost=cost,
    )


def _future_deadline(seconds=300):
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


def _make_coding_service(fake_llm, settings):
    """Build the service over the *real* executor, as ADR-017 prescribes."""
    anonymizer = FakeAnonymizer()
    executor = LLMCallExecutor(
        llm=fake_llm,
        anonymizer=anonymizer,
        settings=settings,
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=MAX_TOKENS,
    )
    return CodingService(llm=fake_llm, anonymizer=anonymizer, executor=executor)


def _make_coding_request(
    feedback_record=None,
    root_codes=None,
    max_codes=5,
    confidence_threshold=None,
    tenant_id=TENANT_ID,
):
    if feedback_record is None:
        feedback_record = _make_feedback_record()
    if root_codes is None:
        root_codes = [CodingNode(id="code-1", name="Code A")]
    return CodingAssignmentRequestModel(
        feedback_record=feedback_record,
        coding_levels=CodingFramework(root_codes=root_codes),
        max_codes=max_codes,
        confidence_threshold=confidence_threshold,
        tenant_id=tenant_id,
    )


class TestAssignCodesOneShot:
    """The pick step is one shot; judging stays per level (unchanged from before)."""

    @pytest.mark.asyncio
    async def test_pick_is_one_call_but_judging_stays_per_level(self, settings):
        """One pick call selects the whole path; judging still runs once per level."""
        root_codes = [
            CodingNode(
                id="type-a",
                name="Type A",
                children=[
                    CodingNode(
                        id="cat-a1",
                        name="Cat A1",
                        children=[CodingNode(id="code-a1-1", name="Code A1.1")],
                    )
                ],
            )
        ]
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[2])),
                _make_llm_response(
                    structured=JudgeResponse(score=0.95, explanation="Level 1 fits.")
                ),
                _make_llm_response(
                    structured=JudgeResponse(score=0.9, explanation="Level 2 fits.")
                ),
                _make_llm_response(
                    structured=JudgeResponse(score=0.8, explanation="Level 3 fits.")
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(root_codes=root_codes), _future_deadline()
        )

        assert len(fake_llm.calls) == 4
        assert fake_llm.calls[0]["response_model"] is CodingResponse
        assert [c["response_model"] for c in fake_llm.calls[1:]] == [JudgeResponse] * 3
        code = result.coded_feedback_records[0].assigned_codes[0]
        assert code.coding_level_1_id == "type-a"
        assert code.coding_level_2_id == "cat-a1"
        assert code.coding_level_3_id == "code-a1-1"
        assert code.confidence_level_1 == 0.95
        assert code.confidence_level_2 == 0.9
        assert code.confidence_level_3 == 0.8
        assert code.confidence_aggregate == 0.8

    @pytest.mark.asyncio
    async def test_selecting_a_non_leaf_option_leaves_deeper_levels_null(
        self, settings
    ):
        """A level-1-only (non-leaf) selection is a valid final answer, not a partial pick.

        Only that one level gets judged — there is no deeper level to score.
        """
        root_codes = [
            CodingNode(
                id="type-a",
                name="Type A",
                children=[CodingNode(id="cat-a1", name="Cat A1")],
            )
        ]
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[0])),
                _make_llm_response(
                    structured=JudgeResponse(score=0.8, explanation="General fit.")
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(root_codes=root_codes), _future_deadline()
        )

        assert len(fake_llm.calls) == 2
        code = result.coded_feedback_records[0].assigned_codes[0]
        assert code.coding_level_1_id == "type-a"
        assert code.coding_level_2_id is None
        assert code.confidence_level_2 is None

    @pytest.mark.asyncio
    async def test_out_of_range_and_duplicate_indices_are_ignored(self, settings):
        """Bad indices from the pick (out of range or repeated) are dropped, not errors.

        Only the one surviving unique index gets judged.
        """
        root_codes = [CodingNode(id="code-1", name="Code A")]
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[0, 0, 99])),
                _make_llm_response(
                    structured=JudgeResponse(score=0.9, explanation="Fits.")
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(root_codes=root_codes), _future_deadline()
        )

        assert len(fake_llm.calls) == 2
        assigned = result.coded_feedback_records[0].assigned_codes
        assert len(assigned) == 1
        assert assigned[0].explanation == "- Level 1 (0.90): Fits."

    @pytest.mark.asyncio
    async def test_malformed_pick_response_degrades_to_nothing_selected(self, settings):
        """A pick response that fails schema validation is a genuine empty pick.

        Not a request failure — matching the old per-level pick step's
        tolerance for malformed LLM output. No judge call follows since
        nothing was selected.
        """
        root_codes = [CodingNode(id="code-1", name="Code A")]
        fake_llm = FakeLLMPort(
            errors=[LLMResponseParseError("LLM response validation failed")]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(root_codes=root_codes), _future_deadline()
        )

        assert len(fake_llm.calls) == 1
        assigned = result.coded_feedback_records[0].assigned_codes
        assert len(assigned) == 1
        assert assigned[0].coding_level_1_id is None
        assert assigned[0].explanation == NO_CODING_NOTHING_RELEVANT_EXPLANATION

    @pytest.mark.asyncio
    async def test_malformed_pick_response_is_logged(self, settings, caplog):
        """A parse failure is distinguishable from a genuine empty pick in the logs.

        Why: without a log line, an operator sees only a spike in
        ``NO CODING APPLIED`` responses and has no way to tell a broken
        prompt/schema apart from records that are genuinely uncodeable.
        """
        root_codes = [CodingNode(id="code-1", name="Code A")]
        fake_llm = FakeLLMPort(
            errors=[LLMResponseParseError("LLM response validation failed")]
        )
        service = _make_coding_service(fake_llm, settings)

        with caplog.at_level(logging.WARNING):
            await service.assign_codes(
                _make_coding_request(root_codes=root_codes), _future_deadline()
            )

        assert any(
            "Coding pick call failed to parse" in record.message
            and "LLMResponseParseError" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_judge_score_out_of_range_raises_analysis_error(self, settings):
        """An out-of-range judge score is a domain error, not a generic LLM failure.

        Unchanged from the previous per-level design: the judge call — not
        the pick call — is where confidence is produced and validated.
        """
        root_codes = [CodingNode(id="code-1", name="Code A")]
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[0])),
                _make_llm_response(
                    structured=JudgeResponse(score=1.5, explanation="Too confident.")
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        with pytest.raises(AnalysisError, match=r"outside 0\.0-1\.0"):
            await service.assign_codes(
                _make_coding_request(root_codes=root_codes), _future_deadline()
            )

    @pytest.mark.asyncio
    async def test_judging_stops_at_the_first_rejected_level(self, settings):
        """A path rejected at level 1 is never judged at level 2.

        Mirrors the previous per-level traversal's early-stop-on-rejection:
        a sub-threshold level is the reason a candidate was dropped, so
        descending further would waste a call and add noise to the
        rejection explanation.
        """
        root_codes = [
            CodingNode(
                id="type-a",
                name="Type A",
                children=[CodingNode(id="cat-a1", name="Cat A1")],
            )
        ]
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[1])),
                _make_llm_response(
                    structured=JudgeResponse(score=0.05, explanation="Weak fit.")
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(root_codes=root_codes, confidence_threshold=0.5),
            _future_deadline(),
        )

        assert len(fake_llm.calls) == 2
        assigned = result.coded_feedback_records[0].assigned_codes
        assert "Weak fit." in assigned[0].explanation


def _make_scored_code(names, scores, explanations):
    """Build a ``_ScoredCode`` from level names, scores and explanations."""
    return _ScoredCode(
        path=[(f"id-{name}", name) for name in names],
        scores=list(scores),
        explanations=list(explanations),
    )


class TestNoCodingAppliedMessage:
    """Formatting of the ``NO CODING APPLIED.`` below-threshold explanation."""

    def test_message_matches_the_documented_layout(self):
        """Pin the whole rendered message against the format agreed in #256.

        Asserting the exact string (rather than a handful of substrings) is
        what makes this the contract: lead line, threshold sentence, blank
        line separators, ``name — percentage`` headers, indented decisive
        explanations and the trailing remainder count.
        """
        rejected = [
            _make_scored_code(
                ["Shelter", "Repairs", "Roofing"],
                [0.8, 0.5, 0.04],
                [
                    "Housing is mentioned.",
                    "Repairs are plausible.",
                    "No mention of roof damage; the feedback concerns rent costs.",
                ],
            ),
            _make_scored_code(["Water"], [0.03], ["No reference to water access."]),
            _make_scored_code(
                ["Food Security"], [0.02], ["Concerns housing, not food."]
            ),
            *[
                _make_scored_code([f"Other {i}"], [0.01], [f"Unrelated {i}."])
                for i in range(5)
            ],
        ]

        message = _combine_rejected_explanations(rejected, threshold=0.1)

        assert message == (
            "NO CODING APPLIED.\n"
            "No code reached the 10% confidence threshold, so this record "
            "needs human review.\n"
            "\n"
            "Shelter > Repairs > Roofing — 4%\n"
            "  No mention of roof damage; the feedback concerns rent costs.\n"
            "\n"
            "Water — 3%\n"
            "  No reference to water access.\n"
            "\n"
            "Food Security — 2%\n"
            "  Concerns housing, not food.\n"
            "\n"
            "5 further codes scored below 2%."
        )

    def test_candidates_are_listed_highest_scoring_first(self):
        """Order by score descending so the closest near-miss is read first.

        The traversal appends rejections in framework order, not score
        order, so the formatter must sort rather than rely on input order.
        """
        rejected = [
            _make_scored_code(["Low"], [0.01], ["Barely related."]),
            _make_scored_code(["High"], [0.09], ["Almost made it."]),
        ]

        message = _combine_rejected_explanations(rejected, threshold=0.1)

        assert message.index("High — 9%") < message.index("Low — 1%")

    def test_remainder_line_is_absent_when_three_or_fewer_rejected(self):
        """No "further codes" line when the list is already complete.

        A count line reading "0 further codes" would be noise; the absence
        of the line is what tells the reader nothing was truncated.
        """
        rejected = [
            _make_scored_code([f"Code {i}"], [0.01 * i], [f"Weak {i}."])
            for i in range(1, 4)
        ]

        message = _combine_rejected_explanations(rejected, threshold=0.1)

        assert "further code" not in message

    def test_remainder_line_is_singular_for_exactly_one_extra(self):
        """Use "1 further code", not "1 further codes", in user-facing text."""
        rejected = [
            _make_scored_code([f"Code {i}"], [0.09 - i / 100], [f"Weak {i}."])
            for i in range(4)
        ]

        message = _combine_rejected_explanations(rejected, threshold=0.1)

        assert message.endswith("1 further code scored below 7%.")

    def test_only_the_decisive_level_explanation_is_shown(self):
        """Show the level that caused rejection, not one line per level.

        The traversal stops descending at the first sub-threshold level, so
        the last accumulated level is both the lowest-scoring one and the
        reason the candidate was dropped. The earlier levels passed and
        would only add noise.
        """
        rejected = [
            _make_scored_code(
                ["Shelter", "Roofing"],
                [0.8, 0.04],
                ["Housing is mentioned.", "No mention of roof damage."],
            )
        ]

        message = _combine_rejected_explanations(rejected, threshold=0.1)

        assert "No mention of roof damage." in message
        assert "Housing is mentioned." not in message

    def test_confidences_render_as_whole_percentages_not_decimals(self):
        """Percentages read naturally to non-technical EspoCRM users.

        The pre-#256 format emitted raw ``0.04``-style decimals next to a
        "Level 2" label, which users had to mentally convert.
        """
        rejected = [_make_scored_code(["Water"], [0.04], ["No water mentioned."])]

        message = _combine_rejected_explanations(rejected, threshold=0.15)

        assert "4%" in message
        assert "15%" in message
        assert "0.04" not in message


class TestAssignCodesConfidenceThreshold:
    @pytest.mark.asyncio
    async def test_all_candidates_rejected_returns_null_codes_with_explanation(
        self, settings
    ):
        """Confirm the near-miss fallback replaces an unexplained empty list.

        When confidence_threshold filters out every candidate, the result
        carries one null-coded entry explaining the rejected candidate
        rather than an unexplained empty list.
        """
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[0])),
                _make_llm_response(
                    structured=JudgeResponse(
                        score=0.5, explanation="Only loosely related."
                    )
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(confidence_threshold=0.9), _future_deadline()
        )

        assigned = result.coded_feedback_records[0].assigned_codes
        assert len(assigned) == 1
        code = assigned[0]
        assert code.coding_level_1_id is None
        assert code.coding_level_1_name is None
        assert code.confidence_level_1 is None
        assert code.confidence_aggregate is None
        assert "Only loosely related." in code.explanation

    @pytest.mark.asyncio
    async def test_llm_never_picks_anything_explains_that_nothing_was_relevant(
        self, settings
    ):
        """Confirm a genuine empty pick is explained rather than returned bare.

        A genuine empty pick (no threshold rejection ever happened) used to
        return an empty list, which left EspoCRM with nothing to show while
        still marking the record completed (#256). It now returns a single
        null-coded entry whose explanation distinguishes "nothing was
        relevant" from the near-miss case.
        """
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=CodingResponse(selected=[]))]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(confidence_threshold=0.9), _future_deadline()
        )

        assigned = result.coded_feedback_records[0].assigned_codes
        assert len(assigned) == 1
        assert assigned[0].coding_level_1_id is None
        assert assigned[0].confidence_aggregate is None
        assert assigned[0].explanation == NO_CODING_NOTHING_RELEVANT_EXPLANATION
        assert assigned[0].explanation.startswith("NO CODING APPLIED.\n")

    @pytest.mark.asyncio
    async def test_all_rejected_explanations_are_combined_highest_first(self, settings):
        """Confirm every rejected branch's explanation is surfaced.

        With two root candidates both rejected, both explanations appear in
        the combined result, higher-scoring rejection first.
        """
        root_codes = [
            CodingNode(id="code-1", name="Code A"),
            CodingNode(id="code-2", name="Code B"),
        ]
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=CodingResponse(selected=[0, 1])),
                _make_llm_response(
                    structured=JudgeResponse(score=0.4, explanation="Weak fit A.")
                ),
                _make_llm_response(
                    structured=JudgeResponse(score=0.7, explanation="Weak fit B.")
                ),
            ]
        )
        service = _make_coding_service(fake_llm, settings)

        result = await service.assign_codes(
            _make_coding_request(root_codes=root_codes, confidence_threshold=0.9),
            _future_deadline(),
        )

        code = result.coded_feedback_records[0].assigned_codes[0]
        assert "Weak fit A." in code.explanation
        assert "Weak fit B." in code.explanation
        assert code.explanation.index("Weak fit B.") < code.explanation.index(
            "Weak fit A."
        )
