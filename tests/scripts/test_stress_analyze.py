"""Tests for the pure helpers in ``scripts/stress_analyze.py``.

The script's networked surface (``run_batch``, ``_post_once``) requires
a live server and is exercised manually. The deterministic helpers
(``load_sample``, ``build_request``, ``summarize``) carry the bulk of
the script's behaviour *and* are imported by
``notebooks/analyze_corpus.ipynb`` — both reasons to lock them down
with unit tests.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

# The ``scripts/`` directory is not a Python package and is not on
# ``sys.path`` by default. Load the module via its file path so the
# tests run regardless of where pytest is invoked from.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "stress_analyze.py"
_spec = importlib.util.spec_from_file_location("stress_analyze", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
stress_analyze = importlib.util.module_from_spec(_spec)
sys.modules["stress_analyze"] = stress_analyze
_spec.loader.exec_module(stress_analyze)


CORPUS_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "analyze_corpus.yaml"


@pytest.fixture(scope="module")
def corpus_size() -> int:
    """Total record count in the fixture, computed once for the module.

    Used by tests that assert ``limit >= len(corpus)`` returns the
    whole corpus without crashing.
    """
    import yaml

    with CORPUS_PATH.open("r", encoding="utf-8") as fp:
        return len(yaml.safe_load(fp))


class TestLoadSample:
    """Deterministic sampling from the corpus YAML."""

    def test_returns_requested_count(self) -> None:
        """``limit`` controls how many records come back."""
        records = stress_analyze.load_sample(CORPUS_PATH, limit=7, seed=1)
        assert len(records) == 7

    def test_seed_is_deterministic(self) -> None:
        """Same seed → same subset, in the same order.

        Determinism is what makes the script comparable across runs:
        if seed 42 produced different records on Tuesday than on
        Monday, comparing latencies would be apples-to-oranges.
        """
        a = stress_analyze.load_sample(CORPUS_PATH, limit=10, seed=42)
        b = stress_analyze.load_sample(CORPUS_PATH, limit=10, seed=42)
        assert [r["id"] for r in a] == [r["id"] for r in b]

    def test_different_seed_yields_different_subset(self) -> None:
        """Two seeds picking 10 records from 5000 should disagree.

        Statistically there is a vanishing chance of accidental
        collision, but with a 5000-record corpus and two arbitrary
        seeds it is below 1e-30 — safe to assert.
        """
        a = stress_analyze.load_sample(CORPUS_PATH, limit=10, seed=42)
        b = stress_analyze.load_sample(CORPUS_PATH, limit=10, seed=99)
        assert [r["id"] for r in a] != [r["id"] for r in b]

    def test_limit_above_corpus_returns_all(self, corpus_size: int) -> None:
        """A ``limit`` past the corpus size returns everything (shuffled).

        Guards against ``random.sample`` raising ``ValueError`` when
        the user asks for more records than exist.
        """
        records = stress_analyze.load_sample(
            CORPUS_PATH, limit=corpus_size + 100, seed=7
        )
        assert len(records) == corpus_size


class TestBuildRequest:
    """Shaping the ``/v1/analyze`` request body."""

    def test_default_body_contains_required_fields(self) -> None:
        """Records, prompt, mode, anonymize are always present."""
        body = stress_analyze.build_request(
            [{"id": "x", "text": "y", "metadata": {}}],
            prompt="why?",
        )
        assert set(body) == {"feedback_records", "prompt", "mode", "anonymize"}
        assert body["mode"] == "hierarchical"
        assert body["anonymize"] is True

    def test_period_only_when_set(self) -> None:
        """Omitting ``period`` keeps the key out of the body.

        Lets the server default kick in; otherwise we'd pin every
        request to whatever the script's local default was.
        """
        without = stress_analyze.build_request([], prompt="p")
        assert "period" not in without

        with_period = stress_analyze.build_request([], prompt="p", period="month")
        assert with_period["period"] == "month"


class TestSummarize:
    """Aggregate statistics across a batch of results."""

    def test_counts_successes_and_failures(self) -> None:
        """Errors are counted as failures regardless of HTTP status.

        A 200 with no parsed body would still set ``error`` — keep the
        truth source for "did this work?" on ``RunResult.error``, not
        on the status code.
        """
        results = [
            stress_analyze.RunResult(status=200, latency_s=0.5),
            stress_analyze.RunResult(status=500, latency_s=0.7, error="HTTP 500"),
            stress_analyze.RunResult(status=None, latency_s=0.1, error="timeout"),
        ]
        summary = stress_analyze.summarize(results)
        assert summary.total == 3
        assert summary.successes == 1
        assert summary.failures == 2
        assert summary.status_counts == Counter({200: 1, 500: 1, None: 1})

    def test_latency_percentiles_are_within_observed_range(self) -> None:
        """Percentiles never go below ``min`` or above ``max``.

        Guards against off-by-one mistakes in the nearest-rank
        percentile arithmetic.
        """
        results = [
            stress_analyze.RunResult(status=200, latency_s=v)
            for v in (0.1, 0.2, 0.5, 1.0, 5.0)
        ]
        summary = stress_analyze.summarize(results)
        assert summary.latency_min == 0.1
        assert summary.latency_max == 5.0
        assert summary.latency_min <= summary.latency_p50 <= summary.latency_max
        assert summary.latency_p50 <= summary.latency_p95 <= summary.latency_p99

    def test_handles_empty_batch(self) -> None:
        """No requests at all → zeroed stats, no crashes.

        Useful when a misconfigured ``--total-calls=0`` slips through.
        """
        summary = stress_analyze.summarize([])
        assert summary.total == 0
        assert summary.latency_p50 == 0.0
