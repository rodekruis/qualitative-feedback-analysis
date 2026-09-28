"""Tests for ``eval/make_analyze_cases.py``.

Each test builds a small, hand-made record and vocabulary list rather
than the real ~110-record pool, so counts and thresholds stay easy to
read. A record only needs the ``id`` and ``metadata`` keys that
``build_answer_key`` reads; missing labels default to ``None`` through
``dict.get``. Fake Langfuse dataset items are ``SimpleNamespace(input=...,
metadata=...)``, since the real ``DatasetItem`` reads both the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from make_analyze_cases import (
    build_answer_key,
    build_cases,
    key_positions,
    record_from_item,
    send_order,
    vocab_from_item,
)
from pool_spec import PROMPTS_DATASET, RECORDS_DATASET, VOCAB_DATASET


def _record(record_id: str, **metadata: Any) -> dict[str, Any]:
    return {"id": record_id, "metadata": metadata}


def _records_with(label: str, counts: dict[str, int]) -> list[dict[str, Any]]:
    """``sum(counts.values())`` records, ``en-0001`` first, in ``counts`` order.

    Every record gets the same ``created``, so ``send_order`` can sort
    them without a ``KeyError`` even though these tests don't care about
    send order.
    """
    records = []
    n = 1
    for value, count in counts.items():
        for _ in range(count):
            records.append(
                _record(f"en-{n:04d}", created="2026-07-01T00:00:00Z", **{label: value})
            )
            n += 1
    return records


def _vocab_entry(
    kind: str,
    name: str,
    keywords: list[str] | None = None,
    issue_keywords: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "keywords": {"en": ["a", "b", "c"] if keywords is None else keywords},
    }
    if issue_keywords is not None:
        entry["issue_keywords"] = {"en": issue_keywords}
    return entry


def test_items_report_counts_required_at_the_threshold_and_keywords() -> None:
    records = [
        *_records_with("need", {"food": 5, "shelter": 3, "health": 2}),
        _record("en-0011"),
        _record("en-0012"),
    ]
    vocab = [
        _vocab_entry("need", "food", keywords=["a", "b", "c"]),
        _vocab_entry("need", "shelter", keywords=["d", "e", "f"]),
        _vocab_entry("need", "health", keywords=["g", "h", "i"]),
    ]

    key = build_answer_key(records, vocab, "needs", "en", required_min=3)

    assert key["items"] == [
        {
            "name": "food",
            "count": 5,
            "required": True,
            "keywords": ["a", "b", "c"],
            "record_ids": [f"en-{i:04d}" for i in range(1, 6)],
        },
        {
            "name": "shelter",
            "count": 3,
            "required": True,
            "keywords": ["d", "e", "f"],
            "record_ids": [f"en-{i:04d}" for i in range(6, 9)],
        },
        {
            "name": "health",
            "count": 2,
            "required": False,
            "keywords": ["g", "h", "i"],
            "record_ids": [f"en-{i:04d}" for i in range(9, 11)],
        },
    ]


def test_protection_key_has_no_items() -> None:
    records = [
        _record(
            "en-0001", theme="protection", urgent_kind="sex_for_aid", keywords=["a"]
        ),
        _record("en-0002", theme="protection", decoy=True, keywords=["b"]),
    ]

    key = build_answer_key(records, [], "protection", "en")

    assert key["items"] == []


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        pytest.param(
            {"a": 30, "b": 20, "c": 13, "d": 10}, ["a", "b", "c"], id="three-items"
        ),
        pytest.param({"a": 10, "b": 9}, None, id="single-item-run"),
    ],
)
def test_rank_checked_stops_where_the_gap_narrows(
    counts: dict[str, int], expected: list[str] | None
) -> None:
    records = _records_with("theme", counts)
    vocab = [_vocab_entry("theme", name) for name in counts]

    key = build_answer_key(records, vocab, "themes", "en", required_min=1)

    assert key["rank_checked"] == expected


def test_rank_checked_only_applies_to_themes_and_needs() -> None:
    records = _records_with("complaint_about", {"a": 30, "b": 20})
    vocab = [_vocab_entry("complaint_about", name) for name in ("a", "b")]

    key = build_answer_key(records, vocab, "complaints", "en", required_min=1)

    assert key["rank_checked"] is None


def test_groups_only_include_records_that_also_set_the_family_label() -> None:
    records = [
        _record("en-0001", need="food", groups=["children"]),
        _record("en-0002", groups=["children"]),  # no need set: excluded
        _record("en-0003", need="shelter", groups=["women"]),
    ]
    vocab = [
        _vocab_entry("need", "food"),
        _vocab_entry("need", "shelter"),
        _vocab_entry("group", "children", issue_keywords=["x", "y", "z"]),
        _vocab_entry("group", "women", issue_keywords=["p", "q", "r"]),
    ]

    key = build_answer_key(records, vocab, "needs", "en", required_min=1)

    assert [g["name"] for g in key["groups"]] == ["children", "women"]
    children = next(g for g in key["groups"] if g["name"] == "children")
    assert children["count"] == 1
    assert children["record_ids"] == ["en-0001"]


def test_groups_for_themes_include_every_group_since_theme_is_always_set() -> None:
    records = [
        _record("en-0001", theme="food", groups=["children"]),
        _record("en-0002", theme="shelter", groups=["women"]),
        _record("en-0003", theme="food"),
    ]
    vocab = [
        _vocab_entry("theme", "food"),
        _vocab_entry("theme", "shelter"),
        _vocab_entry("group", "children", issue_keywords=["x", "y", "z"]),
        _vocab_entry("group", "women", issue_keywords=["p", "q", "r"]),
    ]

    key = build_answer_key(records, vocab, "themes", "en", required_min=1)

    assert {g["name"] for g in key["groups"]} == {"children", "women"}


def test_urgent_and_decoys_are_on_every_key_with_record_keywords() -> None:
    records = [
        _record(
            "en-0001",
            theme="protection",
            urgent_kind="sex_for_aid",
            keywords=["sex", "exchange", "aid"],
        ),
        _record(
            "en-0002",
            theme="protection",
            decoy=True,
            keywords=["altercation", "argument", "conflict"],
        ),
        _record("en-0003", theme="food"),
    ]
    vocab = [_vocab_entry("theme", "food"), _vocab_entry("theme", "protection")]

    key = build_answer_key(records, vocab, "themes", "en", required_min=1)

    assert key["urgent"] == [
        {
            "record_id": "en-0001",
            "kind": "sex_for_aid",
            "keywords": ["sex", "exchange", "aid"],
        }
    ]
    assert key["decoys"] == [
        {"record_id": "en-0002", "keywords": ["altercation", "argument", "conflict"]}
    ]


def test_missing_vocab_entry_for_an_item_raises_with_the_value_name() -> None:
    records = [_record("en-0001", need="food")]

    with pytest.raises(SystemExit, match="food"):
        build_answer_key(records, [], "needs", "en")


def test_missing_vocab_entry_for_a_group_raises_with_the_group_name() -> None:
    records = [_record("en-0001", theme="food", groups=["children"])]
    vocab = [_vocab_entry("theme", "food")]

    with pytest.raises(SystemExit, match="children"):
        build_answer_key(records, vocab, "themes", "en")


def _item_stub(input_: Any, metadata: dict[str, Any]) -> SimpleNamespace:
    """A fake Langfuse dataset item: only the two attributes these functions read."""
    return SimpleNamespace(input=input_, metadata=metadata)


def _prompt(
    prompt_id: str, family: str, *, language: str = "en", text: str = "Prompt text"
) -> SimpleNamespace:
    return _item_stub(
        text, {"prompt_id": prompt_id, "language": language, "family": family}
    )


def test_record_from_item_moves_created_back_into_metadata() -> None:
    item = _item_stub(
        {
            "id": "en-0001",
            "content": "hello",
            "metadata": {"created": "2026-07-01T00:00:00Z"},
        },
        {"theme": "food", "groups": ["children"]},
    )

    assert record_from_item(item) == {
        "id": "en-0001",
        "content": "hello",
        "metadata": {
            "theme": "food",
            "groups": ["children"],
            "created": "2026-07-01T00:00:00Z",
        },
    }


def test_vocab_from_item_carries_issue_keywords_only_when_present() -> None:
    group_item = _item_stub(
        {"keywords": {"en": ["a", "b", "c"]}, "issue_keywords": {"en": ["x", "y"]}},
        {"kind": "group", "name": "children"},
    )
    theme_item = _item_stub(
        {"keywords": {"en": ["a", "b", "c"]}}, {"kind": "theme", "name": "food"}
    )

    assert vocab_from_item(group_item) == {
        "kind": "group",
        "name": "children",
        "keywords": {"en": ["a", "b", "c"]},
        "issue_keywords": {"en": ["x", "y"]},
    }
    assert vocab_from_item(theme_item) == {
        "kind": "theme",
        "name": "food",
        "keywords": {"en": ["a", "b", "c"]},
    }


def test_send_order_sorts_newest_created_first_then_by_id() -> None:
    records = [
        _record("en-0003", created="2026-07-01T00:00:00Z"),
        _record("en-0001", created="2026-08-01T00:00:00Z"),
        _record("en-0002", created="2026-08-01T00:00:00Z"),
    ]

    ordered = send_order(records)

    assert [r["id"] for r in ordered] == ["en-0001", "en-0002", "en-0003"]


def test_key_positions_reports_only_group_and_urgent_positions() -> None:
    key = {
        "items": [{"name": "food", "record_ids": ["en-0001"]}],
        "groups": [{"name": "children", "record_ids": ["en-0003", "en-0001"]}],
        "urgent": [{"record_id": "en-0002"}],
        "decoys": [{"record_id": "en-0001"}],
    }
    record_ids = ["en-0001", "en-0002", "en-0003"]

    assert key_positions(key, record_ids) == {
        "groups:children": [0, 2],
        "urgent:en-0002": [1],
    }


def test_build_cases_builds_one_case_per_english_prompt() -> None:
    records = _records_with("theme", {"food": 5, "shelter": 3})
    vocab = [_vocab_entry("theme", "food"), _vocab_entry("theme", "shelter")]
    prompts = [
        _prompt("P01", "themes", text="Summarise the themes"),
        # A non-English twin: build_cases sends one case per English prompt only.
        _prompt("P01", "themes", language="es", text="Resuma los temas"),
    ]
    read_at = datetime(2026, 9, 29, 9, 12, 44, 512000, tzinfo=UTC)

    cases = build_cases(records, vocab, prompts, read_at)

    assert len(cases) == 1
    [case] = cases
    assert case["id"] == "P01-en"
    assert case["input"] == {
        "prompt_id": "P01",
        "prompt": "Summarise the themes",
        "mode": "single_pass",
        "output_language": "English",
        "record_ids": [r["id"] for r in send_order(records)],
    }
    assert case["expected_output"] == build_answer_key(records, vocab, "themes", "en")
    assert case["metadata"] == {
        "records_dataset": RECORDS_DATASET,
        "records_version": read_at.isoformat(),
        "vocab_dataset": VOCAB_DATASET,
        "vocab_version": read_at.isoformat(),
        "prompt_dataset": PROMPTS_DATASET,
        "prompt_id": "P01",
        "family": "themes",
        "language": "en",
        "key_positions": key_positions(
            case["expected_output"], case["input"]["record_ids"]
        ),
    }


def test_build_cases_stops_on_an_unplanted_prompt() -> None:
    records = _records_with("theme", {"food": 5})
    vocab = [_vocab_entry("theme", "food")]
    prompts = [_prompt("P99", "unplanted")]

    with pytest.raises(SystemExit, match="P99"):
        build_cases(records, vocab, prompts, datetime.now(UTC))


def test_build_cases_stops_when_a_family_has_no_required_item() -> None:
    records = _records_with("theme", {"food": 2})  # below REQUIRED_MIN_RECORDS
    vocab = [_vocab_entry("theme", "food")]
    prompts = [_prompt("P01", "themes")]

    with pytest.raises(SystemExit, match="themes"):
        build_cases(records, vocab, prompts, datetime.now(UTC))
