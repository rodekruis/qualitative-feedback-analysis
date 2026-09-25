"""Tests for the pure checks in ``eval/upload_prompts.py``.

No test here touches Langfuse: ``main()`` is exercised manually
"""

from __future__ import annotations

from typing import Any

import pytest

import upload_prompts

validate_prompts = upload_prompts.validate_prompts
build_items = upload_prompts.build_items


def _prompt(
    id: str = "P01-en", family: str = "themes", **overrides: Any
) -> dict[str, Any]:
    metadata = {
        "prompt_id": id.split("-")[0],
        "language": "en",
        "twin_id": None,
        "family": family,
        "source": "QFA training slides v1",
        "supplied_by": "Daan",
        "supplied_on": "09-09-2026",
        "use_case": "analyze-bulk",
    }
    metadata.update(overrides)
    return {"id": id, "prompt": "Summarise the main themes", "metadata": metadata}


def test_validate_prompts_passes_for_well_formed_records() -> None:
    validate_prompts([_prompt("P01-en", "themes"), _prompt("P02-en", "needs")])


def test_validate_prompts_allows_the_unplanted_family() -> None:
    validate_prompts([_prompt("P11-en", "unplanted")])


def test_validate_prompts_stops_on_a_duplicate_id() -> None:
    with pytest.raises(SystemExit, match="P01-en"):
        validate_prompts([_prompt("P01-en"), _prompt("P01-en")])


def test_validate_prompts_stops_on_an_unknown_family() -> None:
    with pytest.raises(SystemExit, match="not-a-family"):
        validate_prompts([_prompt("P01-en", family="not-a-family")])


def test_validate_prompts_stops_on_a_missing_metadata_field() -> None:
    record = _prompt("P01-en")
    del record["metadata"]["supplied_by"]

    with pytest.raises(SystemExit, match="supplied_by"):
        validate_prompts([record])


def test_build_items_maps_prompt_text_to_input_and_keeps_metadata() -> None:
    [item] = build_items([_prompt("P01-en", "themes")])

    assert item["id"] == "P01-en"
    assert item["input"] == "Summarise the main themes"
    assert item["expected_output"] is None
    assert item["metadata"]["family"] == "themes"
