"""Utility functions for the feedback analysis backend."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from qfa.settings import LogSettings


@dataclass
class Stopwatch:
    """Holds the measured wall-clock duration of a :func:`timed` block."""

    elapsed_seconds: float = 0.0


@contextmanager
def timed() -> Iterator[Stopwatch]:
    """Measure the wall-clock duration of a block, without logging anything.

    Yields a :class:`Stopwatch` whose ``elapsed_seconds`` is populated when the
    block exits — including when it raises, so a phase that fails still reports
    how long it ran. Measurement is deliberately decoupled from logging: the
    caller owns the log message, so it controls the level and guarantees only
    content-free values (counts, latencies) reach the log, per the hard
    prohibitions in ``docs/operations/observability.md``.

    Uses :func:`time.perf_counter` (monotonic) so it is unaffected by wall-clock
    adjustments such as NTP steps.

    Examples
    --------
    Time a phase, then log a content-free summary yourself::

        with timed() as sw:
            vectors = embedder.embed(texts)
        logger.info("embedded %d record(s) in %.2fs", len(texts), sw.elapsed_seconds)
    """
    stopwatch = Stopwatch()
    start = time.perf_counter()
    try:
        yield stopwatch
    finally:
        stopwatch.elapsed_seconds = time.perf_counter() - start


def setup_logging(log_settings: LogSettings | None = None) -> None:
    """Set up the logging system.

    Parameters
    ----------
    log_settings : LogSettings | None
        Optional logging configuration. When ``None`` a default
        ``LogSettings`` instance is created.
    """
    log_config = log_settings or LogSettings()
    # force=True replaces our own stdout handler on repeat calls, but it also
    # removes *and closes* every other root handler — including the OTel
    # export handler that configure_azure_monitor() attached to root earlier in
    # startup. That silently emptied AppTraces (#223). So detach third-party
    # sinks first (a closed exporter handler is unrecoverable) and re-attach
    # them after. logging.FileHandler subclasses StreamHandler, so
    # basicConfig-style output handlers are still replaced as before.
    root = logging.getLogger()
    preserved = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]
    for handler in preserved:
        root.removeHandler(handler)

    logging.basicConfig(
        level=log_config.loglevel_3rdparty, force=True, **log_config.basicConfig
    )

    for handler in preserved:
        root.addHandler(handler)

    our_loglevel = log_config.loglevel
    for package in log_config.our_packages:
        logging.getLogger(package).setLevel(our_loglevel)
