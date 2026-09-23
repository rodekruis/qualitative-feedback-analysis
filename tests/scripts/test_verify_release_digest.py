"""Tests for the deploy-time digest cross-check.

``.github/scripts/verify_release_digest.py`` requires the release body's
digest to agree with the digest resolved from ACR before ``_deploy-release.yaml``
deploys it. This is the fix for #173 (a forged release body could otherwise
point a deploy at any image in ACR), so its decision logic is unit-tested here
rather than left inline in workflow YAML.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# ``.github/scripts/`` is not a Python package and is not on ``sys.path``.
# Load the module by file path (mirrors ``tests/scripts/test_is_latest_release.py``).
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "scripts"
    / "verify_release_digest.py"
)
_spec = importlib.util.spec_from_file_location("verify_release_digest", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
verify_release_digest = importlib.util.module_from_spec(_spec)
sys.modules["verify_release_digest"] = verify_release_digest
_spec.loader.exec_module(verify_release_digest)

verify = verify_release_digest.verify
body_digests = verify_release_digest.body_digests

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def test_body_digest_matching_registry_is_accepted():
    """A release body whose digest matches the registry's passes."""
    body = f"## Deployment\n\n- **Digest**: `{_DIGEST_A}`\n"
    assert verify(body, _DIGEST_A) == _DIGEST_A


def test_prepended_second_digest_aborts():
    """The #173 attack: an attacker prepends a second digest to a published body."""
    body = f"forged: {_DIGEST_B}\n\n## Deployment\n\n- **Digest**: `{_DIGEST_A}`\n"
    with pytest.raises(ValueError):
        verify(body, _DIGEST_A)


def test_repeated_identical_digest_is_not_a_conflict():
    """The same digest appearing twice in the body is not a distinct second digest."""
    body = f"{_DIGEST_A}\n\n## Deployment\n\n- **Digest**: `{_DIGEST_A}`\n"
    assert verify(body, _DIGEST_A) == _DIGEST_A


def test_body_digest_differing_from_registry_aborts():
    """A single body digest that disagrees with the registry aborts (the swap variant)."""
    body = f"## Deployment\n\n- **Digest**: `{_DIGEST_B}`\n"
    with pytest.raises(ValueError):
        verify(body, _DIGEST_A)


def test_body_without_digest_aborts():
    """A body with no digest at all aborts -- no regression from today's grep path."""
    with pytest.raises(ValueError):
        verify("This release has no deployment section yet.", _DIGEST_A)


def test_uppercase_digest_is_not_a_hiding_place():
    """An uppercase SHA256 is still counted as a (conflicting) second digest."""
    body = f"{_DIGEST_A.upper()}\n\n## Deployment\n\n- **Digest**: `{_DIGEST_B}`\n"
    assert body_digests(body) == [_DIGEST_A, _DIGEST_B]
    with pytest.raises(ValueError):
        verify(body, _DIGEST_B)


def test_over_long_hex_run_is_not_truncated_into_a_digest():
    """A run of 65+ hex characters must not be truncated into a valid-looking digest."""
    over_long = "sha256:" + "a" * 65
    assert body_digests(over_long) == []


def test_malformed_registry_digest_aborts():
    """An empty or non-digest registry value aborts before the body is even compared."""
    with pytest.raises(ValueError):
        verify(f"- **Digest**: `{_DIGEST_A}`\n", "")
    with pytest.raises(ValueError):
        verify(f"- **Digest**: `{_DIGEST_A}`\n", "not-a-digest")


def _run_cli(
    tag: str, registry_digest: str, body: str
) -> subprocess.CompletedProcess[str]:
    """Invoke the script the way the workflow does: body on stdin, digest via flag."""
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--tag",
            tag,
            "--registry-digest",
            registry_digest,
        ],
        input=body,
        capture_output=True,
        text=True,
    )


def test_cli_prints_digest_and_exits_zero():
    """CLI prints digest=<registry digest> and exits 0 when the body agrees."""
    body = f"## Deployment\n\n- **Digest**: `{_DIGEST_A}`\n"
    proc = _run_cli("v0.4.0", _DIGEST_A, body)
    assert proc.returncode == 0
    assert f"digest={_DIGEST_A}" in proc.stdout


def test_cli_exits_two_on_conflict():
    """CLI exits 2 and emits an ::error:: annotation when the body disagrees."""
    body = f"## Deployment\n\n- **Digest**: `{_DIGEST_B}`\n"
    proc = _run_cli("v0.4.0", _DIGEST_A, body)
    assert proc.returncode == 2
    assert "::error::" in proc.stderr
    assert "v0.4.0" in proc.stderr
    assert "digest=" not in proc.stdout
