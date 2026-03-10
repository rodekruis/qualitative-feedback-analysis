"""Utility functions for the feedback analysis backend."""

import logging

from qfa.settings import LogSettings


def setup_logging(log_settings: LogSettings | None = None) -> None:
    """Set up the logging system.

    Parameters
    ----------
    log_settings : LogSettings | None
        Optional logging configuration. When ``None`` a default
        ``LogSettings`` instance is created.
    """
    log_config = log_settings or LogSettings()
    logging.basicConfig(level=log_config.loglevel_3rdparty, **log_config.basicConfig)

    our_loglevel = log_config.loglevel
    for package in log_config.our_packages:
        logging.getLogger(package).setLevel(our_loglevel)
