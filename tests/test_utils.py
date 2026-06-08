"""Tests for the timing/logging helpers in ``qfa.utils``."""

import pytest

from qfa import utils
from qfa.utils import Stopwatch, timed


def test_timed_records_elapsed_from_perf_counter(monkeypatch: pytest.MonkeyPatch):
    """``timed()`` reports the monotonic-clock delta between entry and exit.

    Why: the orchestrator's per-phase log lines and the notebook's end-to-end
    wall time both read ``Stopwatch.elapsed_seconds``, so the measured value
    must equal exit-tick minus entry-tick. A fake ``perf_counter`` makes the
    assertion exact rather than timing-dependent (and therefore non-flaky).
    """
    ticks = iter([100.0, 102.5])
    monkeypatch.setattr(utils.time, "perf_counter", lambda: next(ticks))

    with timed() as sw:
        pass

    assert isinstance(sw, Stopwatch)
    assert sw.elapsed_seconds == pytest.approx(2.5)


def test_timed_sets_elapsed_even_when_block_raises(monkeypatch: pytest.MonkeyPatch):
    """``elapsed_seconds`` is populated even when the timed block raises.

    Why: a phase that overruns its deadline (or otherwise fails) should still
    report how long it ran before failing. The value is assigned in a
    ``finally`` block, so the exception propagates while the stopwatch stays
    meaningful for any log line emitted after the error is caught.
    """
    ticks = iter([10.0, 13.0])
    monkeypatch.setattr(utils.time, "perf_counter", lambda: next(ticks))

    with pytest.raises(ValueError):
        with timed() as sw:
            raise ValueError("boom")

    assert sw.elapsed_seconds == pytest.approx(3.0)
