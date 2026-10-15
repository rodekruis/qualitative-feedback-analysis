"""Tests for ``eval/make_analyze_cases.py``.

Each test builds a small, hand-made record list rather than the real
~110-record pool, so counts and thresholds stay easy to read. A record
only needs the ``id`` and ``metadata`` keys that ``build_answer_key``
reads; missing labels default to ``None`` through ``dict.get``. Fake
Langfuse dataset items are ``SimpleNamespace(input=..., metadata=...)``,
since the real ``DatasetItem`` reads both the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from make_analyze_cases import (
    _relabel_key,
    build_answer_key,
    build_cases,
    check_translation,
    record_from_item,
    send_order,
)
from pool_spec import PROMPTS_DATASET, RECORDS_DATASETS


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


def test_labels_hold_every_record_and_required_starts_at_the_threshold() -> None:
    records = [
        *_records_with("need", {"food": 3, "shelter": 2}),
        _record("en-0006"),
    ]

    key = build_answer_key(records, "needs", required_min=3)

    assert key["labels"] == {
        "en-0001": "food",
        "en-0002": "food",
        "en-0003": "food",
        "en-0004": "shelter",
        "en-0005": "shelter",
        "en-0006": None,
    }
    assert key["required"] == ["food"]


def test_only_the_protection_key_has_urgent() -> None:
    records = [
        _record("en-0001", theme="protection", urgent_kind="sex_for_aid"),
        _record("en-0002", theme="protection", decoy=True),
        _record("en-0003", theme="food"),
    ]

    protection = build_answer_key(records, "protection")
    themes = build_answer_key(records, "themes", required_min=1)

    assert protection["urgent"] == ["en-0001"]
    assert "labels" not in protection
    assert "required" not in protection
    assert "urgent" not in themes


def test_groups_only_include_records_that_also_set_the_family_label() -> None:
    records = [
        _record("en-0001", need="food", groups=["children"]),
        _record("en-0002", groups=["children"]),  # no need set: excluded
        _record("en-0003", need="shelter", groups=["women"]),
    ]

    key = build_answer_key(records, "needs", required_min=1)

    assert key["groups"] == {"children": ["en-0001"], "women": ["en-0003"]}


def test_groups_for_themes_include_every_group_since_theme_is_always_set() -> None:
    records = [
        _record("en-0001", theme="food", groups=["children"]),
        _record("en-0002", theme="shelter", groups=["women"]),
        _record("en-0003", theme="food"),
    ]

    key = build_answer_key(records, "themes", required_min=1)

    assert set(key["groups"]) == {"children", "women"}


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


def test_send_order_sorts_newest_created_first_then_by_id() -> None:
    records = [
        _record("en-0003", created="2026-07-01T00:00:00Z"),
        _record("en-0001", created="2026-08-01T00:00:00Z"),
        _record("en-0002", created="2026-08-01T00:00:00Z"),
    ]

    ordered = send_order(records)

    assert [r["id"] for r in ordered] == ["en-0001", "en-0002", "en-0003"]


def test_build_cases_builds_one_case_per_english_prompt() -> None:
    records = _records_with("theme", {"food": 5, "shelter": 3})
    prompts = [
        _prompt("P01", "themes", text="Summarise the themes"),
        # A non-English twin: build_cases sends one case per English prompt only.
        _prompt("P01", "themes", language="es", text="Resuma los temas"),
    ]
    read_at = datetime(2026, 9, 29, 9, 12, 44, 512000, tzinfo=UTC)

    cases = build_cases(records, prompts, read_at)

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
    assert case["expected_output"] == build_answer_key(records, "themes")
    assert case["metadata"] == {
        "records_dataset": RECORDS_DATASETS["en"],
        "records_version": read_at.isoformat(),
        "prompt_dataset": PROMPTS_DATASET,
        "prompt_id": "P01",
        "family": "themes",
        "language": "en",
    }


def test_build_cases_stops_on_an_unplanted_prompt() -> None:
    records = _records_with("theme", {"food": 5})
    prompts = [_prompt("P99", "unplanted")]

    with pytest.raises(SystemExit, match="P99"):
        build_cases(records, prompts, datetime.now(UTC))


def test_build_cases_stops_when_a_family_has_no_required_item() -> None:
    records = _records_with("theme", {"food": 2})  # below REQUIRED_MIN_RECORDS
    prompts = [_prompt("P01", "themes")]

    with pytest.raises(SystemExit, match="themes"):
        build_cases(records, prompts, datetime.now(UTC))


def _es_record(record_id: str, base_id: str, **metadata: Any) -> dict[str, Any]:
    return _record(
        record_id, base_id=base_id, created="2026-07-01T00:00:00Z", **metadata
    )


def _pair() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A three-record English pool and its Spanish translation."""
    base = [
        _record("en-0001", created="2026-07-01T00:00:00Z", theme="food", need="food"),
        _record(
            "en-0002",
            created="2026-07-02T00:00:00Z",
            theme="food",
            need="food",
            groups=["children", "women"],
        ),
        _record(
            "en-0003",
            created="2026-07-03T00:00:00Z",
            theme="protection",
            urgent_kind="sex_for_aid",
        ),
    ]
    translated = [
        _es_record("es-0001", "en-0001", theme="food", need="food"),
        _es_record(
            "es-0002",
            "en-0002",
            theme="food",
            need="food",
            groups=["women", "children"],
        ),
        _es_record("es-0003", "en-0003", theme="protection", urgent_kind="sex_for_aid"),
    ]
    return translated, base


def test_check_translation_passes_when_every_label_is_copied() -> None:
    """Also covers the per-family key comparison, protection's ``urgent`` included."""
    translated, base = _pair()

    assert check_translation(translated, base) is None


@pytest.mark.parametrize(
    ("change", "match"),
    [
        pytest.param(
            lambda rs: rs[0]["metadata"].pop("base_id"), "es-0001", id="missing-base-id"
        ),
        pytest.param(
            lambda rs: rs[0]["metadata"].update(base_id="en-9999"),
            "en-9999",
            id="unknown-base-id",
        ),
        pytest.param(
            lambda rs: rs[1]["metadata"].update(base_id="en-0001"),
            "used twice",
            id="shared-base-id",
        ),
        pytest.param(
            lambda rs: rs.pop(), "en-0003", id="base-record-with-no-translation"
        ),
        pytest.param(
            lambda rs: rs[0]["metadata"].update(theme="shelter"),
            "es-0001: theme",
            id="changed-label",
        ),
        pytest.param(
            lambda rs: rs[1]["metadata"].update(groups=["children"]),
            "es-0002: groups",
            id="changed-groups",
        ),
    ],
)
def test_check_translation_stops_on_a_broken_pair(change: Any, match: str) -> None:
    translated, base = _pair()
    change(translated)

    with pytest.raises(SystemExit, match=match):
        check_translation(translated, base)


def test_relabel_key_maps_labels_groups_and_urgent() -> None:
    key = {
        "family": "needs",
        "labels": {"es-0001": "food", "es-0002": None},
        "required": ["food"],
        "groups": {"children": ["es-0002", "es-0001"]},
        "urgent": ["es-0002"],
    }

    assert _relabel_key(key, {"es-0001": "en-0001", "es-0002": "en-0002"}) == {
        "family": "needs",
        "labels": {"en-0001": "food", "en-0002": None},
        "required": ["food"],
        "groups": {"children": ["en-0001", "en-0002"]},
        "urgent": ["en-0002"],
    }


def test_build_cases_builds_the_spanish_case_set_from_the_spanish_prompts() -> None:
    records = _records_with("theme", {"food": 5, "shelter": 3})
    prompts = [
        _prompt("P01", "themes", text="Summarise the themes"),
        _prompt("P01", "themes", language="es", text="Resuma los temas"),
    ]
    read_at = datetime(2026, 9, 29, 9, 12, 44, 512000, tzinfo=UTC)

    [case] = build_cases(records, prompts, read_at, "es")

    assert case["id"] == "P01-es"
    assert case["input"]["prompt"] == "Resuma los temas"
    assert case["input"]["output_language"] == "Spanish"
    assert case["metadata"]["records_dataset"] == RECORDS_DATASETS["es"]
    assert case["metadata"]["language"] == "es"


def test_build_cases_stops_when_no_prompt_is_in_the_language() -> None:
    records = _records_with("theme", {"food": 5})

    with pytest.raises(SystemExit, match="no prompt in es"):
        build_cases(records, [_prompt("P01", "themes")], datetime.now(UTC), "es")


def test_build_cases_stops_on_a_half_translated_prompt_set() -> None:
    records = _records_with("theme", {"food": 5})
    prompts = [
        _prompt("P01", "themes"),
        _prompt("P02", "themes"),
        _prompt("P01", "themes", language="es"),
    ]

    with pytest.raises(SystemExit, match="missing P02"):
        build_cases(records, prompts, datetime.now(UTC), "es")
