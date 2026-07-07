"""Tests for the release-publish guard's version comparison.

``.github/scripts/is_latest_release.py`` decides whether a just-published
release is the greatest semver among all releases (drafts included). It is the
only automated safety mechanism gating auto-deploy on publish, so its decision
logic is unit-tested here rather than left inline (and untestable) in workflow
YAML. Scenario numbers below map to the table in
``docs/specs/guard-auto-deploy-on-publish.md``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# ``.github/scripts/`` is not a Python package and is not on ``sys.path``.
# Load the module by file path so the tests run regardless of where pytest is
# invoked from (mirrors ``tests/scripts/test_stress_analyze.py``).
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "is_latest_release.py"
)
_spec = importlib.util.spec_from_file_location("is_latest_release", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
is_latest_release = importlib.util.module_from_spec(_spec)
sys.modules["is_latest_release"] = is_latest_release
_spec.loader.exec_module(is_latest_release)

is_latest = is_latest_release.is_latest


def test_greatest_including_drafts_returns_true():
    """A tag that outranks every other release is the latest (scenario 1)."""
    assert is_latest("v0.6.0", ["v0.3.0", "v0.6.0", "v0.5.0"]) is True


def test_newer_published_release_returns_false():
    """A newer published release makes an older tag not-latest (scenario 2)."""
    assert is_latest("v0.5.0", ["v0.5.0", "v0.6.0"]) is False


def test_newer_draft_release_returns_false():
    """A newer *draft* fed into the tag list by the workflow blocks deploy (scenario 3).

    The script is draft-agnostic: the workflow lists drafts via ``gh release
    list``, so a newer unpublished draft appears here as an ordinary tag. This
    is the load-bearing case — publishing an older draft must not deploy while a
    newer draft exists.
    """
    assert is_latest("v0.5.0", ["v0.5.0", "v0.6.0"]) is False


def test_prerelease_below_its_final_returns_false():
    """Publishing rc.1 while the final release exists must not deploy (scenario 4a)."""
    assert is_latest("v1.0.0-rc.1", ["v1.0.0-rc.1", "v1.0.0"]) is False


def test_final_above_its_prerelease_returns_true():
    """A final release outranks its own pre-release (scenario 4b)."""
    assert is_latest("v1.0.0", ["v1.0.0-rc.1", "v1.0.0"]) is True


def test_later_rc_outranks_earlier_rc():
    """rc.2 is newer than rc.1 — semver-aware, unlike ``sort -V`` (scenario 5)."""
    assert is_latest("v0.6.0-rc.2", ["v0.6.0-rc.1", "v0.6.0-rc.2"]) is True


def test_only_release_is_latest():
    """The sole release is trivially the latest (scenario 6)."""
    assert is_latest("v0.4.0", ["v0.4.0"]) is True


def test_unparseable_tag_raises():
    """An unparseable tag makes the answer undecidable -> fail closed (scenario 7)."""
    with pytest.raises(ValueError):
        is_latest("v0.4.0", ["v0.4.0", "not-a-version"])


def test_tag_absent_from_list_raises():
    """A tag missing from the known releases is a fault -> fail closed (scenario 8)."""
    with pytest.raises(ValueError):
        is_latest("v9.9.9", ["v0.4.0", "v0.5.0"])


def _run_cli(tag: str, all_tags: str) -> subprocess.CompletedProcess[str]:
    """Invoke the script the way the workflow does, capturing stdout/stderr/exit."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--tag", tag, "--all-tags", all_tags],
        capture_output=True,
        text=True,
    )


def test_cli_exits_zero_and_reports_true():
    """CLI prints is_greatest=true and the latest tag, exit 0, for the latest tag."""
    proc = _run_cli("v0.6.0", "v0.5.0,v0.6.0")
    assert proc.returncode == 0
    assert "is_greatest=true" in proc.stdout
    assert "latest=v0.6.0" in proc.stdout


def test_cli_exits_zero_and_reports_false():
    """CLI prints is_greatest=false, exit 0, when a newer release exists."""
    proc = _run_cli("v0.5.0", "v0.5.0,v0.6.0")
    assert proc.returncode == 0
    assert "is_greatest=false" in proc.stdout


def test_cli_fails_closed_on_error():
    """CLI exits 2 and emits an ::error:: annotation when latest is undecidable."""
    proc = _run_cli("v0.4.0", "v0.4.0,not-a-version")
    assert proc.returncode == 2
    assert "::error::" in proc.stderr
