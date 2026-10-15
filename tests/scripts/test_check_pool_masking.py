"""Tests for the pure helpers in ``scripts/check_pool_masking.py``.

``main()`` loads the real Presidio anonymizer and spaCy models, so it is
run by hand, never in ``make test``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# ``scripts/`` is not a package and is not on ``sys.path``; load the module
# by file path so the tests run from anywhere. The ``qfa`` import the script
# needs sits inside ``main()``, so importing it here pulls in nothing heavy.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_pool_masking.py"
_spec = importlib.util.spec_from_file_location("check_pool_masking", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_pool_masking = importlib.util.module_from_spec(_spec)
sys.modules["check_pool_masking"] = check_pool_masking
_spec.loader.exec_module(check_pool_masking)

_select_prompts = check_pool_masking._select_prompts


def _prompt(prompt_id: str, language: str) -> dict[str, Any]:
    return {
        "id": f"{prompt_id}-{language}",
        "prompt": "Prompt text",
        "metadata": {"prompt_id": prompt_id, "language": language},
    }


_PROMPTS = [_prompt("P01", "en"), _prompt("P01", "es"), _prompt("P02", "en")]


def test_no_language_keeps_every_prompt() -> None:
    assert _select_prompts(_PROMPTS, None) == _PROMPTS


def test_a_language_keeps_only_its_own_prompts() -> None:
    assert [p["id"] for p in _select_prompts(_PROMPTS, "es")] == ["P01-es"]


def test_a_language_with_no_prompt_stops_rather_than_reporting_a_clean_run() -> None:
    with pytest.raises(SystemExit, match="no prompt in fr"):
        _select_prompts(_PROMPTS, "fr")
