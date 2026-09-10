"""Tests for the timing/logging helpers in ``qfa.utils``."""

import logging
from collections.abc import Iterator

import pytest

from qfa import utils
from qfa.utils import Stopwatch, setup_logging, timed


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


class _RecordingHandler(logging.Handler):
    """Stand-in for a third-party sink, e.g. the OTel log-export handler.

    A bare ``logging.Handler`` on purpose: that is what
    ``azure-monitor-opentelemetry`` attaches to the root logger, and being
    outside the ``StreamHandler`` hierarchy is exactly what
    ``setup_logging`` keys off.
    """

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def emit(self, record: logging.LogRecord) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        super().close()


@pytest.fixture
def clean_root_logger() -> Iterator[logging.Logger]:
    """Restore the root logger's handlers, which ``setup_logging`` rewrites."""
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    for handler in original:
        root.removeHandler(handler)
    yield root
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original:
        root.addHandler(handler)
    root.setLevel(original_level)


def test_setup_logging_preserves_non_stream_root_handlers(
    clean_root_logger: logging.Logger,
):
    """A third-party root handler survives ``setup_logging`` intact.

    Why: ``configure_azure_monitor()`` attaches its log-export handler to the
    root logger before the lifespan runs. ``basicConfig(force=True)`` used to
    remove *and close* it, which silently emptied ``AppTraces`` — every log
    call still succeeded, so nothing surfaced the loss (#223). Asserting it was
    never closed matters as much as asserting it is still attached: a closed
    exporter handler cannot be reattached.
    """
    otel_handler = _RecordingHandler()
    clean_root_logger.addHandler(otel_handler)

    setup_logging()

    assert otel_handler in clean_root_logger.handlers
    assert otel_handler.closed is False


def test_setup_logging_still_replaces_the_stream_handler(
    clean_root_logger: logging.Logger,
):
    """Repeat calls must not stack up duplicate stdout handlers.

    This is the behaviour ``force=True`` was there for in the first place, and
    it has to keep working now that handlers are preserved selectively.
    """
    setup_logging()
    setup_logging()

    stream_handlers = [
        h for h in clean_root_logger.handlers if isinstance(h, logging.StreamHandler)
    ]
    assert len(stream_handlers) == 1
