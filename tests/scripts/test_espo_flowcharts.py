r"""Guard the EspoCRM flowchart payload builders (issue #245).

The CSV export is the only maintained copy of each flow's formula
script, and nothing in CI can execute EspoCRM Formula Script. These
tests pin the one property that broke production: every request body is
serialised by ``json\encode``, never assembled by hand.
"""

import csv
import json
import re
from pathlib import Path

import pytest

FLOWCHART_DIR = Path(__file__).parents[2] / "scripts" / "espo_crm" / "flowcharts"
FLOWCHARTS = sorted(FLOWCHART_DIR.glob("*.csv"))


def _nodes(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1, f"{path.name}: expected a single exported flowchart row"
    return json.loads(rows[0]["data"])["list"]


def _actions(path: Path, action_type: str):
    for node in _nodes(path):
        for action in node.get("actionList", []):
            if action.get("type") == action_type:
                yield node, action


def _formulas(path: Path):
    return [action["formula"] for _, action in _actions(path, "executeFormula")]


@pytest.mark.parametrize("path", FLOWCHARTS, ids=lambda p: p.name)
def test_no_json_literals_in_formulas(path):
    """A JSON brace inside a formula means a payload is being hand-assembled."""
    offenders = [
        formula
        for formula in _formulas(path)
        if any(marker in formula for marker in ("'{\"", "'{'", '"{'))
    ]
    assert offenders == [], (
        f"{path.name}: a formula assembles JSON from string literals. Build an "
        "object with object\\create() / list() and serialise it with "
        "json\\encode() instead."
    )


_REPLACE_CALL = re.compile(
    r"string\\replace\([^,]+,\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
)

# What json\encode() already escapes correctly: control characters, quotes,
# and backslashes. A formula stripping one of these targets is redoing the
# serialiser's job by hand, the exact issue #245 bug. Replacing anything
# else (e.g. formatting an ID) is an unrelated, legitimate transformation.
_ESCAPE_SENSITIVE_TARGETS = {"\\n", "\\r", "\\t", "\\f", "\\v", "\\\\", '"', "'"}


def _replace_search_targets(formula: str):
    r"""Unquoted search argument of each ``string\replace()`` call."""
    return [literal[1:-1] for literal in _REPLACE_CALL.findall(formula)]


@pytest.mark.parametrize("path", FLOWCHARTS, ids=lambda p: p.name)
def test_feedback_text_is_not_rewritten(path):
    """Escaping is the serialiser's job; stripping destroys feedback text."""
    offenders = [
        formula
        for formula in _formulas(path)
        if any(
            target in _ESCAPE_SENSITIVE_TARGETS
            for target in _replace_search_targets(formula)
        )
    ]
    assert offenders == [], (
        f"{path.name}: a formula strips a character json\\encode() already "
        "escapes correctly (a control character, a quote, or a backslash). "
        "Stripping it by hand silently mangles what a beneficiary wrote."
    )


@pytest.mark.parametrize("path", FLOWCHARTS, ids=lambda p: p.name)
def test_request_bodies_come_from_json_encode(path):
    r"""Every ``sendRequest`` body variable is assigned from ``json\encode``."""
    formulas = _formulas(path)
    for node, action in _actions(path, "sendRequest"):
        name = action["contentVariable"].lstrip("$")
        assigned = re.compile(rf"\${{1,2}}{re.escape(name)}\s*=\s*json\\encode\(")
        assert any(assigned.search(formula) for formula in formulas), (
            f"{path.name}: node {node['id']} posts ${name}, which no formula "
            "assigns from json\\encode()."
        )


def test_community_meeting_flowchart_sends_meeting_base_url():
    formulas = "\n".join(
        _formulas(FLOWCHART_DIR / "Community_meeting_summary_flowchart.csv")
    )
    assert "$payload['espo_meeting_base_url'] = $espoBaseUrl" in formulas
    assert "espo_feedback_base_url" not in formulas


@pytest.mark.parametrize("level", [1, 2, 3])
def test_feedback_flowchart_includes_optional_code_descriptions(level):
    nodes = _nodes(FLOWCHART_DIR / "Feedback_saving_flowchart.csv")
    framework = next(node for node in nodes if node["id"] == "g3dcslcmvk")
    formula = framework["actionList"][0]["formula"]

    lookup = (
        f"$desc{level} = record\\attribute("
        f"'CCodingLevel{level}', $id{level}, 'description');"
    )
    append = (
        f"ifThen($desc{level} != null && string\\length($desc{level}) > 0, "
        f"$name{level} = string\\concatenate($name{level}, ' ', $desc{level}));"
    )
    assignment = f"$node{level}['name'] = $name{level};"

    assert lookup in formula
    assert append in formula
    assert formula.index(lookup) < formula.index(append) < formula.index(assignment)


def test_feedback_flowchart_uses_configurable_confidence_thresholds():
    flowchart = FLOWCHART_DIR / "Feedback_saving_flowchart.csv"
    nodes = {node["id"]: node for node in _nodes(flowchart)}
    coding = nodes["pgang8h4rp"]["actionList"][1]["formula"]
    sensitive = nodes["xqfh7up3zb"]["actionList"][1]["formula"]

    for level in (1, 2, 3):
        assert f"assigned_codes.0.confidence_level_{level}" in coding
    for level, weight in ((1, "0.20"), (2, "0.30"), (3, "0.50")):
        assert f"$confidenceLevel{level} != null, {weight}, 0" in coding
    assert "/ $totalWeight" in coding
    assert "$confidenceLevel3 ?? $confidenceLevel2 ?? $confidenceLevel1" in coding
    assert "QFA_CODING_WEIGHTED_THRESHOLD" in coding
    assert "QFA_CODING_DEEPEST_THRESHOLD" in coding
    assert "$weightedConfidenceScore < $weightedThreshold" in coding
    assert "$deepestLevelConfidence < $deepestLevelThreshold" in coding
    assert "confidence_aggregate" not in coding
    assert "autoCodingStatus = 'human_review_required'" in coding

    assert "QFA_SENSITIVE_CONFIDENCE_THRESHOLD" in sensitive
    assert "$sensitiveConfidence == null" in sensitive
    assert "$sensitiveConfidence < $sensitiveThreshold" in sensitive
    assert "autoSensitiveStatus = 'human_review_required'" in sensitive


def test_feedback_flowchart_uses_optional_minimum_coding_threshold():
    nodes = {
        node["id"]: node
        for node in _nodes(FLOWCHART_DIR / "Feedback_saving_flowchart.csv")
    }
    payload = nodes["7k24bkfmi6"]["actionList"][1]["formula"]

    assert "$minimumThreshold = 0.1;" in payload
    assert (
        "if (record\\exists('AppSecret', 'name=', "
        "'QFA_CODING_MINIMUM_THRESHOLD')) {\n"
        "    $minimumThreshold = number\\parseFloat("
        "ext\\appSecret\\get('QFA_CODING_MINIMUM_THRESHOLD'));\n"
        "}"
    ) in payload
    assert "$payload['confidence_threshold'] = $minimumThreshold;" in payload
    assert "$payload['confidence_threshold'] = 0.1;" not in payload
    assert "string\\trim(" not in payload
    assert "string\\replace(" not in payload
    assert "string\\test(" not in payload


def test_insight_flowchart_sends_meetings_to_both_bulk_endpoints():
    """Meeting records are collected regardless of the insight method."""
    flowchart = FLOWCHART_DIR / "Insight_creation_flowchart.csv"
    nodes = _nodes(flowchart)
    collection = next(node for node in nodes if node["id"] == "ski0qsd6qx")
    formula = collection["actionList"][0]["formula"]

    assert "$analysisRecords = list();" in formula
    assert "$feedbackRecord['record_type'] = 'feedback';" in formula
    assert (
        "$analysisRecords = array\\push($analysisRecords, $feedbackRecord);" in formula
    )
    assert (
        "$analysisRecords = array\\push($analysisRecords, $meetingRecord);" in formula
    )
    assert "$feedbackRecords" not in formula
    assert "$meetingCount = array\\length($$meetingBackendIDs);" in formula
    assert "$method ==" not in formula
    assert "ifThen($meetingNotes == null" not in formula


def test_insight_flowchart_preserves_trace_and_meeting_links():
    """Insight responses retain Langfuse IDs and separate meeting link bases."""
    flowchart = FLOWCHART_DIR / "Insight_creation_flowchart.csv"
    nodes = _nodes(flowchart)
    payload = next(node for node in nodes if node["id"] == "rzhpnsh7eu")
    response = next(node for node in nodes if node["id"] == "wkr4x6yr9y")
    payload_formula = payload["actionList"][0]["formula"]
    response_formula = next(
        action["formula"]
        for action in response["actionList"]
        if action["type"] == "executeFormula"
    )

    assert "QFA_ESPO_MEETING_BASE_URL" in payload_formula
    assert "$payload['espo_meeting_base_url'] = $espoMeetingBaseUrl;" in payload_formula
    assert "requestID = json\\retrieve($response, 'request_id');" in response_formula


def test_langfuse_flow_requires_non_empty_request_id():
    """Do not submit Langfuse scores without a usable trace ID."""
    flowchart = FLOWCHART_DIR / "user-feedback-to-Langfuse.csv"
    nodes = _nodes(flowchart)
    start = next(node for node in nodes if node["id"] == "9ozig1mlsx")
    payload = next(node for node in nodes if node["id"] == "aub7jtft8l")
    condition_items = [
        condition for group in start["conditionsAll"] for condition in group["value"]
    ]
    request_id_values = {
        condition["value"]
        for condition in condition_items
        if condition.get("fieldToCompare") == "requestID"
    }
    formula = next(
        action["formula"]
        for action in payload["actionList"]
        if action["type"] == "executeFormula"
    )

    assert None in request_id_values
    assert "" in request_id_values
    assert "ifThen($request_id == null, $request_id = '');" in formula
