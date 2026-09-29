"""Tests for the pure helpers in ``eval/analyze_eval.py``.

No test here calls the network: the task itself (``_make_analyze``) is
only checked for its ``async`` shape, since exercising an HTTP call
would need a live backend. Fake Langfuse items are
``SimpleNamespace(input=..., metadata=...)``, matching how the real
``DatasetItem`` is read.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import analyze_eval
from analyze_eval import (
    _make_analyze,
    build_request_body,
    load_records,
    outcome_of,
    resolve_records,
    save_answers,
)


def _case(records_dataset: str, records_version: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={
            "records_dataset": records_dataset,
            "records_version": records_version,
        }
    )


def _record_item(record_id: str, content: str, created: str) -> SimpleNamespace:
    return SimpleNamespace(
        input={"id": record_id, "content": content, "metadata": {"created": created}}
    )


@dataclass
class _FakeDataset:
    items: list[Any]


@dataclass
class _FakeLangfuse:
    """Records each ``get_dataset`` call so a test can assert on the calls made."""

    datasets: dict[tuple[str, datetime], _FakeDataset]
    calls: list[tuple[str, datetime]] = field(default_factory=list)

    def get_dataset(self, name: str, *, version: datetime) -> _FakeDataset:
        self.calls.append((name, version))
        return self.datasets[(name, version)]


def test_load_records_fetches_each_records_dataset_version_pair_once() -> None:
    version = datetime(2026, 9, 29, 9, 12, 44, 512000, tzinfo=UTC)
    version_str = version.isoformat()
    item_a = _record_item("en-0001", "hello", "2026-07-01T00:00:00Z")
    item_b = _record_item("en-0002", "world", "2026-07-02T00:00:00Z")
    fake = _FakeLangfuse(
        {("feedback/records-en-v1", version): _FakeDataset([item_a, item_b])}
    )
    cases = [
        _case("feedback/records-en-v1", version_str),
        _case("feedback/records-en-v1", version_str),  # same pair: fetched once
    ]

    records = load_records(fake, cases)

    assert fake.calls == [("feedback/records-en-v1", version)]
    assert records == {
        ("feedback/records-en-v1", version_str): {
            "en-0001": item_a.input,
            "en-0002": item_b.input,
        }
    }


def test_resolve_records_keeps_order_and_raises_on_an_unknown_id() -> None:
    records = {"en-0001": {"id": "en-0001"}, "en-0002": {"id": "en-0002"}}

    assert resolve_records(records, ["en-0002", "en-0001"]) == [
        {"id": "en-0002"},
        {"id": "en-0001"},
    ]

    with pytest.raises(KeyError, match="en-0099"):
        resolve_records(records, ["en-0099"])


def test_build_request_body_sends_only_the_allowed_keys() -> None:
    records = [
        {
            "id": "en-0001",
            "content": "hello",
            "metadata": {"created": "2026-07-01T00:00:00Z", "coding_level_1": "Food"},
        }
    ]

    body = build_request_body("Summarise", "single_pass", "English", records)

    assert body == {
        "prompt": "Summarise" + analyze_eval.CITE_RECORDS_INSTRUCTION,
        "mode": "single_pass",
        "output_language": "English",
        "feedback_records": [
            {
                "id": "en-0001",
                "content": "hello",
                "metadata": {"created": "2026-07-01T00:00:00Z"},
            }
        ],
    }
    assert "anonymize" not in body
    assert "espo_feedback_base_url" not in body
    assert "url_id" not in body
    assert "coding_level_1" not in body["feedback_records"][0]["metadata"]


def test_the_citation_instruction_names_no_label_and_no_example_id() -> None:
    """It must not tell the model what to find, nor seed a record id."""
    instruction = analyze_eval.CITE_RECORDS_INSTRUCTION.lower()

    assert not re.search(r"\b[a-z]{2}-\d{4}\b", instruction)
    assert not any(word in instruction for word in ("food", "shelter", "theme", "need"))


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        pytest.param(200, {}, "ok", id="200"),
        pytest.param(413, None, "payload_too_large", id="413"),
        pytest.param(
            422,
            {"error": {"code": "content_policy_violation"}},
            "content_filtered",
            id="422-content-policy-violation",
        ),
        pytest.param(
            422,
            {"error": {"code": "validation_error"}},
            "error",
            id="422-validation-error",
        ),
        pytest.param(500, None, "error", id="500"),
    ],
)
def test_outcome_of(status: int, body: dict[str, Any] | None, expected: str) -> None:
    assert outcome_of(status, body) == expected


def test_make_analyze_returns_a_coroutine_function() -> None:
    """A sync task would make Langfuse run every case one at a time."""
    task = _make_analyze("http://example.test", "key", {})

    assert inspect.iscoroutinefunction(task)


def _item_result(prompt_id: str, output: dict[str, Any]) -> SimpleNamespace:
    item = SimpleNamespace(
        input={"prompt_id": prompt_id, "record_ids": ["en-0001", "en-0002"]},
        metadata={"language": "en"},
        expected_output={"family": "needs", "required": ["food"]},
    )
    return SimpleNamespace(item=item, output=output)


def test_save_answers_writes_one_line_per_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyze_eval, "ANSWERS_DIR", tmp_path / "analyze-runs")
    item_results = [
        _item_result("P02", {"outcome": "ok", "analysis": "## Food\nen-0001."}),
        _item_result("P03", {"outcome": "payload_too_large", "status": 413}),
    ]

    path = save_answers(item_results, "cases-frequent-en-v1-full-20260929-120000")

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {
        "case": "P02-en",
        "outcome": "ok",
        "analysis": "## Food\nen-0001.",
        "key": {"family": "needs", "required": ["food"]},
        "record_ids": ["en-0001", "en-0002"],
    }
    # A case with no answer still gets a line, so every case is accounted for.
    assert lines[1]["case"] == "P03-en"
    assert lines[1]["analysis"] is None
