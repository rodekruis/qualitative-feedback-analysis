"""Tests for the derived constants in ``eval/pool_spec.py``."""

from __future__ import annotations

from pool_spec import FAMILY_LABEL, LABEL_KEYS


def test_label_keys_cover_every_family_label_and_groups() -> None:
    every_label = {label for labels in FAMILY_LABEL.values() for label in labels}

    assert set(LABEL_KEYS) == every_label | {"groups"}
