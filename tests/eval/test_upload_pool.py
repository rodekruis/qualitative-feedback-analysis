"""Tests for the pure checks in ``eval/upload_pool.py``.

No test here touches Langfuse: ``main()`` needs real credentials and
writes to a shared dataset, so it is exercised manually (a dry run, then a
real upload), never in ``make test``. Each test uses a short, hand-made
record list rather than the real ~110-record pool, so ``planted`` and
``decoys`` are passed in to match.
"""

from __future__ import annotations

from typing import Any

import pytest

import upload_pool

validate_pool = upload_pool.validate_pool
build_items = upload_pool.build_items
validate_vocab = upload_pool.validate_vocab
build_vocab_items = upload_pool.build_vocab_items

_TEXT = "x" * 80


def _metadata(**overrides: Any) -> dict[str, Any]:
    metadata = {
        "theme": "food",
        "need": None,
        "complaint_about": None,
        "rumour": None,
        "suggestion": None,
        "praise_about": None,
        "info_request": None,
        "urgent_kind": None,
        "decoy": False,
        "promise_gap": None,
        "access_barrier": None,
        "groups": [],
        "keywords": [],
        "facts": [],
        "language": "en",
        "created": "2026-07-01T00:00:00Z",
        "gen_model": None,
        "gen_date": None,
        "review_status": None,
    }
    metadata.update(overrides)
    return metadata


def _record(
    record_id: str = "en-0001", content: str = "", **meta: Any
) -> dict[str, Any]:
    return {"id": record_id, "content": content, "metadata": _metadata(**meta)}


def _missing_facts() -> dict[str, Any]:
    record = _record("en-0001")
    del record["metadata"]["facts"]
    return record


def _vocab_entry(
    kind: str = "theme",
    name: str = "food",
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


@pytest.mark.parametrize(
    ("records", "kwargs", "match"),
    [
        pytest.param(
            [_record("bad-id")],
            {"planted": {}, "decoys": 0},
            "en-NNNN",
            id="ill-shaped-id",
        ),
        pytest.param(
            [_record("en-0001"), _record("en-0001")],
            {"planted": {}, "decoys": 0},
            "en-0001",
            id="duplicate-id",
        ),
        pytest.param(
            [_missing_facts()],
            {"planted": {}, "decoys": 0},
            "facts",
            id="missing-metadata-field",
        ),
        pytest.param(
            [_record("en-0001", theme="food"), _record("en-0002", theme="food")],
            {"planted": {"theme": {"food": 1}}, "decoys": 0},
            "theme",
            id="planted-count",
        ),
        pytest.param(
            [_record("en-0001")],
            {"planted": {}, "decoys": 1},
            "decoys",
            id="decoy-count",
        ),
        pytest.param(
            [
                _record("en-0001", theme="protection", urgent_kind="sex_for_aid"),
                _record("en-0002", theme="protection", urgent_kind="sex_for_aid"),
            ],
            {"planted": {}, "decoys": 0},
            "share the same urgent_kind",
            id="shared-urgent-kind",
        ),
        pytest.param(
            [_record("en-0001", theme="food", urgent_kind="sex_for_aid")],
            {"planted": {"urgent_kind": {"sex_for_aid": 1}}, "decoys": 0},
            "protection",
            id="urgent-not-protection-theme",
        ),
        pytest.param(
            [_record("en-0001", created="2026-01-01T00:00:00Z")],
            {"planted": {}, "decoys": 0},
            "en-0001",
            id="created-outside-window",
        ),
        pytest.param(
            [_record("en-0001", content="short", facts=["one fact"])],
            {"planted": {}, "decoys": 0},
            "60-1200",
            id="text-too-short",
        ),
        pytest.param(
            [_record("en-0001", content=_TEXT, facts=[])],
            {"planted": {}, "decoys": 0},
            "facts",
            id="text-without-facts",
        ),
        pytest.param(
            [
                _record(
                    "en-0001",
                    content="We began the referral process for the family. " * 2,
                    facts=["f"],
                )
            ],
            {"planted": {}, "decoys": 0},
            "referral",
            id="banned-word",
        ),
        pytest.param(
            [
                _record(
                    "en-0001",
                    theme="protection",
                    urgent_kind="sex_for_aid",
                    content=_TEXT,
                    facts=["f"],
                    keywords=["only", "two"],
                )
            ],
            {"planted": {"urgent_kind": {"sex_for_aid": 1}}, "decoys": 0},
            "keywords",
            id="urgent-lacks-keywords",
        ),
        pytest.param(
            [_record("en-0001")],
            {"planted": {}, "decoys": 0, "final": True},
            "no text",
            id="final-missing-text",
        ),
        pytest.param(
            [_record("en-0001", content=_TEXT, facts=["f"])],
            {"planted": {}, "decoys": 0, "final": True},
            "review_status",
            id="final-missing-review-status",
        ),
        pytest.param(
            [
                _record(
                    "en-0001",
                    theme="protection",
                    urgent_kind="sex_for_aid",
                    content=_TEXT,
                    facts=["f"],
                    keywords=["a", "b", "c"],
                    review_status="not_reviewed",
                )
            ],
            {
                "planted": {"urgent_kind": {"sex_for_aid": 1}},
                "decoys": 0,
                "final": True,
            },
            r'review_status: "reviewed"',
            id="final-urgent-not-reviewed",
        ),
        pytest.param(
            [
                _record(
                    "en-0001",
                    theme="protection",
                    decoy=True,
                    content=_TEXT,
                    facts=["f"],
                    keywords=["a", "b", "c"],
                    review_status="not_reviewed",
                )
            ],
            {"planted": {}, "decoys": 1, "final": True},
            r'review_status: "reviewed"',
            id="final-decoy-not-reviewed",
        ),
        pytest.param(
            [
                _record(
                    "en-0001",
                    groups=["children"],
                    content=_TEXT,
                    facts=["f"],
                    review_status="not_reviewed",
                )
            ],
            {"planted": {"groups": {"children": 1}}, "decoys": 0, "final": True},
            r'review_status: "reviewed"',
            id="final-group-not-reviewed",
        ),
    ],
)
def test_validate_pool_rejects(
    records: list[dict[str, Any]], kwargs: dict[str, Any], match: str
) -> None:
    with pytest.raises(SystemExit, match=match):
        validate_pool(records, **kwargs)


def test_validate_pool_passes_for_a_well_formed_minimal_pool(
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = [_record(f"en-{i:04d}", theme="food") for i in range(1, 6)] + [
        _record(f"en-{i:04d}", theme="shelter") for i in range(6, 11)
    ]

    validate_pool(records, planted={"theme": {"food": 5, "shelter": 5}}, decoys=0)

    assert "10 records have no text yet" in capsys.readouterr().out


def test_validate_pool_final_accepts_text_and_review_status() -> None:
    records = [
        _record(
            f"en-{i:04d}",
            content=_TEXT,
            facts=["one fact"],
            review_status="not_reviewed",
            theme="food",
        )
        for i in range(1, 3)
    ]

    validate_pool(records, planted={"theme": {"food": 2}}, decoys=0, final=True)


def test_validate_pool_final_accepts_a_reviewed_urgent_record() -> None:
    records = [
        _record(
            "en-0001",
            theme="protection",
            urgent_kind="sex_for_aid",
            content=_TEXT,
            facts=["one fact"],
            keywords=["a", "b", "c"],
            review_status="reviewed",
        )
    ]

    validate_pool(
        records,
        planted={"theme": {"protection": 1}, "urgent_kind": {"sex_for_aid": 1}},
        decoys=0,
        final=True,
    )


@pytest.mark.parametrize(
    ("entries", "records", "vocab_labels", "match"),
    [
        pytest.param(
            [],
            [_record("en-0001", theme="food")],
            {"theme": "theme"},
            "no vocabulary entry for food",
            id="missing-entry-for-a-record-value",
        ),
        pytest.param(
            [
                _vocab_entry(kind="theme", name="food"),
                _vocab_entry(kind="theme", name="shelter"),
            ],
            [_record("en-0001", theme="food")],
            {"theme": "theme"},
            "shelter",
            id="orphan-entry-matches-no-record",
        ),
        pytest.param(
            [_vocab_entry(kind="theme", name="food", keywords=["a", "b"])],
            [_record("en-0001", theme="food")],
            {"theme": "theme"},
            "fewer than 3 keywords",
            id="entry-lacks-keywords",
        ),
        pytest.param(
            [_vocab_entry(kind="group", name="children", issue_keywords=["x"])],
            [_record("en-0001", groups=["children"])],
            {"groups": "group"},
            "fewer than 3 issue_keywords",
            id="group-entry-lacks-issue-keywords",
        ),
        pytest.param(
            [
                _vocab_entry(
                    kind="group",
                    name="unaccompanied_minors",
                    keywords=["minor", "separated", "unaccompanied"],
                    issue_keywords=["a", "b", "c"],
                )
            ],
            [_record("en-0001", groups=["unaccompanied_minors"])],
            {"groups": "group"},
            "stoplisted word 'minor'",
            id="minor-theme-keyword-on-stoplist",
        ),
        pytest.param(
            [
                _vocab_entry(
                    kind="group",
                    name="children",
                    keywords=["kid", "x", "y"],
                    issue_keywords=["p", "q", "r"],
                ),
                _vocab_entry(
                    kind="group",
                    name="women",
                    keywords=["kid", "z", "w"],
                    issue_keywords=["p", "q", "r"],
                ),
            ],
            [
                _record("en-0001", groups=["children"]),
                _record("en-0002", groups=["women"]),
            ],
            {"groups": "group"},
            "shared by children and women",
            id="two-group-entries-share-a-keyword",
        ),
        pytest.param(
            [],
            [
                _record(
                    "en-0001",
                    theme="protection",
                    urgent_kind="sex_for_aid",
                    keywords=["a", "b", "c"],
                ),
                _record(
                    "en-0002",
                    theme="protection",
                    urgent_kind="trafficking_risk",
                    keywords=["a", "d", "e"],
                ),
            ],
            {},
            "urgent keyword 'a' shared by en-0001 and en-0002",
            id="two-urgent-records-share-a-keyword",
        ),
        pytest.param(
            [],
            [
                _record(
                    "en-0001",
                    theme="protection",
                    urgent_kind="sex_for_aid",
                    content=_TEXT,
                    facts=["f"],
                    keywords=["a distinctive phrase", "b", "c"],
                ),
                _record(
                    "en-0002",
                    theme="protection",
                    decoy=True,
                    content="This decoy text contains a distinctive phrase in passing.",
                    facts=["f"],
                ),
            ],
            {},
            "urgent keyword 'a distinctive phrase' appears in decoy en-0002",
            id="urgent-keyword-appears-in-decoy-text",
        ),
    ],
)
def test_validate_vocab_rejects(
    entries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    vocab_labels: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(SystemExit, match=match):
        validate_vocab(entries, records, vocab_labels=vocab_labels)


def test_validate_vocab_passes_for_a_well_formed_minimal_vocabulary() -> None:
    entries = [
        _vocab_entry(kind="theme", name="food"),
        _vocab_entry(kind="group", name="children", issue_keywords=["x", "y", "z"]),
    ]
    records = [
        _record("en-0001", theme="food"),
        _record("en-0002", groups=["children"]),
    ]

    validate_vocab(entries, records, vocab_labels={"theme": "theme", "groups": "group"})


def test_build_vocab_items_splits_keywords_into_input_and_tags_into_metadata() -> None:
    entry = _vocab_entry(
        kind="group",
        name="children",
        keywords=["a", "b", "c"],
        issue_keywords=["d", "e", "f"],
    )

    [item] = build_vocab_items([entry])

    assert item["id"] == "group:children"
    assert item["input"] == {
        "keywords": {"en": ["a", "b", "c"]},
        "issue_keywords": {"en": ["d", "e", "f"]},
    }
    assert item["expected_output"] is None
    assert item["metadata"] == {"kind": "group", "name": "children"}


def test_build_items_moves_created_into_input_metadata_and_keeps_the_rest() -> None:
    record = _record(
        "en-0001", content="hello", theme="food", created="2026-07-01T00:00:00Z"
    )

    [item] = build_items([record])

    assert item["id"] == "en-0001"
    assert item["input"] == {
        "id": "en-0001",
        "content": "hello",
        "metadata": {"created": "2026-07-01T00:00:00Z"},
    }
    assert item["expected_output"] is None
    assert "created" not in item["metadata"]
    assert item["metadata"]["theme"] == "food"
