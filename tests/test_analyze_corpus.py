"""Loader and invariants test for ``fixtures/analyze_corpus.yaml``.

The corpus is a hand-/LLM-generated set of fake community feedback records
intended as input for the ``/v1/analyze`` endpoint. Each item must parse into
``FeedbackRecordModel`` and the codes embedded in ``metadata.codes`` must be
consistent with the coding framework shipped at ``fixtures/coding_framework.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from qfa.domain.models import FeedbackRecordModel

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CORPUS_PATH = FIXTURES / "analyze_corpus.yaml"
FRAMEWORK_PATH = FIXTURES / "coding_framework.json"

MIN_SENTENCES = 1
MAX_SENTENCES = 15


def _flatten_framework(framework: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten the nested framework JSON into a ``{code_id: {feedback_type, sensitive}}`` lookup."""
    flat: dict[str, dict[str, Any]] = {}
    for type_node in framework["types"]:
        ft = type_node["name"]
        for cat in type_node["categories"]:
            for code in cat["codes"]:
                flat[code["code_id"]] = {
                    "feedback_type": ft,
                    "sensitive": code.get("sensitive", False),
                }
    return flat


def _count_sentences(text: str) -> int:
    """Approximate sentence count using terminator split (matches the corpus spec)."""
    return len([p for p in re.split(r"[.!?]+", text) if p.strip()])


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    """Load the analyze corpus YAML once per test module."""
    with CORPUS_PATH.open() as f:
        data = yaml.safe_load(f)
    assert isinstance(data, list), "corpus must be a top-level YAML list"
    return data


@pytest.fixture(scope="module")
def framework() -> dict[str, dict[str, Any]]:
    """Flattened coding framework keyed by ``code_id``."""
    with FRAMEWORK_PATH.open() as f:
        return _flatten_framework(json.load(f))


class TestAnalyzeCorpus:
    def test_corpus_is_non_empty(self, corpus: list[dict[str, Any]]) -> None:
        """Sanity: corpus has at least one record so the other tests have anything to assert against."""
        assert len(corpus) > 0

    def test_every_record_parses_into_feedback_record_model(
        self, corpus: list[dict[str, Any]]
    ) -> None:
        """Every record must round-trip through ``FeedbackRecordModel`` so the corpus is ready to feed the analyze endpoint without further transformation."""
        for item in corpus:
            FeedbackRecordModel(**item)

    def test_all_ids_are_unique(self, corpus: list[dict[str, Any]]) -> None:
        """Duplicate IDs would silently collapse to one record in any dict-keyed aggregation downstream."""
        ids = [item["id"] for item in corpus]
        assert len(ids) == len(set(ids)), "duplicate ids in corpus"

    def test_every_code_id_exists_in_framework(
        self,
        corpus: list[dict[str, Any]],
        framework: dict[str, dict[str, Any]],
    ) -> None:
        """Detect orphan/invented code IDs early — labels must reference real framework codes, not paraphrases."""
        unknown: dict[str, list[str]] = {}
        for item in corpus:
            code_ids = [
                c.strip() for c in item["metadata"]["codes"].split(",") if c.strip()
            ]
            missing = [c for c in code_ids if c not in framework]
            if missing:
                unknown[item["id"]] = missing
        assert not unknown, f"records reference unknown code_ids: {unknown}"

    def test_metadata_feedback_type_matches_assigned_codes(
        self,
        corpus: list[dict[str, Any]],
        framework: dict[str, dict[str, Any]],
    ) -> None:
        """``metadata.feedback_type`` must equal the feedback_type of every assigned code; otherwise the record is internally inconsistent."""
        mismatches: list[str] = []
        for item in corpus:
            md = item["metadata"]
            code_ids = [c.strip() for c in md["codes"].split(",") if c.strip()]
            code_types = {framework[c]["feedback_type"] for c in code_ids}
            if code_types != {md["feedback_type"]}:
                mismatches.append(
                    f"{item['id']}: metadata={md['feedback_type']!r} codes={code_types}"
                )
        assert not mismatches, "feedback_type mismatches:\n" + "\n".join(mismatches)

    def test_sensitive_flag_matches_assigned_codes(
        self,
        corpus: list[dict[str, Any]],
        framework: dict[str, dict[str, Any]],
    ) -> None:
        """``metadata.sensitive`` must be True iff at least one assigned code is marked sensitive in the framework."""
        mismatches: list[str] = []
        for item in corpus:
            md = item["metadata"]
            code_ids = [c.strip() for c in md["codes"].split(",") if c.strip()]
            expected = any(framework[c]["sensitive"] for c in code_ids)
            if md["sensitive"] != expected:
                mismatches.append(
                    f"{item['id']}: metadata={md['sensitive']} framework={expected}"
                )
        assert not mismatches, "sensitive flag mismatches:\n" + "\n".join(mismatches)

    def test_sentence_count_is_in_range(self, corpus: list[dict[str, Any]]) -> None:
        """The corpus contract is 1-15 sentences per record; outside that range it's not the intended shape."""
        out_of_range = [
            item["id"]
            for item in corpus
            if not (
                MIN_SENTENCES <= item["metadata"]["sentence_count"] <= MAX_SENTENCES
            )
        ]
        assert not out_of_range, f"sentence_count out of [1,15]: {out_of_range}"

    def test_sentence_count_matches_text_within_tolerance(
        self, corpus: list[dict[str, Any]]
    ) -> None:
        """``metadata.sentence_count`` should be within ±1 of the terminator-split count of ``text``; larger drift means the label has lost touch with the content."""
        drift: list[str] = []
        for item in corpus:
            declared = item["metadata"]["sentence_count"]
            actual = _count_sentences(item["text"])
            if abs(declared - actual) > 1:
                drift.append(f"{item['id']}: declared={declared} actual={actual}")
        assert not drift, "sentence_count drift > 1:\n" + "\n".join(drift)

    def test_codes_string_has_no_whitespace_around_separators(
        self, corpus: list[dict[str, Any]]
    ) -> None:
        """The corpus spec defines ``codes`` as a comma-joined string with no spaces; whitespace around separators would break naive splits in downstream consumers."""
        offenders = [
            item["id"]
            for item in corpus
            if ", " in item["metadata"]["codes"] or " ," in item["metadata"]["codes"]
        ]
        assert not offenders, f"codes string has whitespace around comma: {offenders}"

    def test_every_record_has_at_least_one_code(
        self, corpus: list[dict[str, Any]]
    ) -> None:
        """A record with no codes provides no ground-truth signal; such records should have been dropped by the repair pass."""
        empty = [
            item["id"]
            for item in corpus
            if not [c for c in item["metadata"]["codes"].split(",") if c.strip()]
        ]
        assert not empty, f"records with empty codes: {empty}"
