"""Shared test setup for ``tests/eval/``.

``eval/`` is not a package: its scripts import sibling modules as
``from _common import ...``, which only resolves when ``eval/`` is on
``sys.path``. Running a script directly puts it there (``sys.path[0]``);
running its tests under pytest does not, so add it here once for every
test in this directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"

if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))
