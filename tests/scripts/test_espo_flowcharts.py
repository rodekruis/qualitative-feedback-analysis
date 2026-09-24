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
