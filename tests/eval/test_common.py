"""Tests for the shared helpers in ``eval/_common.py``.

``eval/`` is not a package. Load the module by file path so the tests run
regardless of where pytest is invoked from (mirrors
``tests/scripts/test_stress_analyze.py``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "eval" / "_common.py"
_spec = importlib.util.spec_from_file_location("_common", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_common = importlib.util.module_from_spec(_spec)
sys.modules["_common"] = _common
_spec.loader.exec_module(_common)

DEFAULT_BASE_URL = _common.DEFAULT_BASE_URL
deployed_info = _common.deployed_info
git_metadata = _common.git_metadata
prompt_versions = _common.prompt_versions
resolve_api_key = _common.resolve_api_key
resolve_config = _common.resolve_config
run_metadata = _common.run_metadata

_EVAL_ENV = (
    "QFA_DEV_API_KEY",
    "QFA_API_BASE_URL",
    "AUTH_API_KEYS",
    "GIT_SHA",
    "GIT_REF_NAME",
)


@pytest.fixture(autouse=True)
def _isolate_eval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip eval env vars so a developer's shell or ``.env`` cannot leak in."""
    for key in _EVAL_ENV:
        monkeypatch.delenv(key, raising=False)


def test_resolve_api_key_prefers_qfa_dev_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QFA_DEV_API_KEY", "from-dev-key")
    monkeypatch.setenv(
        "AUTH_API_KEYS",
        json.dumps([{"name": "local", "key": "from-auth-json"}]),
    )

    assert resolve_api_key() == "from-dev-key"


def test_resolve_api_key_falls_back_to_first_plaintext_auth_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTH_API_KEYS",
        json.dumps(
            [
                {"name": "hashed-only", "hashed_key": "abc"},
                {"name": "local", "key": "first-plaintext"},
                {"name": "other", "key": "second-plaintext"},
            ]
        ),
    )

    assert resolve_api_key() == "first-plaintext"


def test_resolve_api_key_exits_when_neither_source_is_set() -> None:
    with pytest.raises(SystemExit, match="QFA_DEV_API_KEY"):
        resolve_api_key()


def test_resolve_api_key_exits_when_every_auth_entry_is_hashed_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTH_API_KEYS",
        json.dumps(
            [
                {"name": "a", "hashed_key": "abc"},
                {"name": "b", "hashed_key": "def"},
            ]
        ),
    )

    with pytest.raises(SystemExit, match="hashed_key"):
        resolve_api_key()


def test_resolve_config_defaults_base_url_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QFA_DEV_API_KEY", "dev-key")

    assert resolve_config() == (DEFAULT_BASE_URL, "dev-key")


def test_resolve_config_uses_env_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QFA_API_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("QFA_DEV_API_KEY", "dev-key")

    assert resolve_config() == ("http://localhost:8000", "dev-key")


def test_resolve_config_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QFA_API_BASE_URL", "http://localhost:8000/")
    monkeypatch.setenv("QFA_DEV_API_KEY", "dev-key")

    assert resolve_config() == ("http://localhost:8000", "dev-key")


def test_git_metadata_prefers_env_over_local_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "ci-sha")
    monkeypatch.setenv("GIT_REF_NAME", "ci-branch")
    monkeypatch.setattr(_common, "_git_output", lambda *_args: "local-value")

    assert git_metadata() == {"eval_script_sha": "ci-sha", "git_branch": "ci-branch"}


def test_run_metadata_carries_contract_keys_and_merges_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "script-sha")
    monkeypatch.setenv("GIT_REF_NAME", "feat/x")
    monkeypatch.setattr(
        _common,
        "deployed_info",
        lambda _base_url: {
            "deployed_version": "1.2.3",
            "deployed_commit": "deploy-sha",
        },
    )

    meta = run_metadata(
        "https://example.invalid",
        dataset="sensitivity/foo",
        limit=2,
    )

    assert meta["eval_script_sha"] == "script-sha"
    assert meta["git_branch"] == "feat/x"
    assert meta["deployed_version"] == "1.2.3"
    assert meta["deployed_commit"] == "deploy-sha"
    assert meta["dataset"] == "sensitivity/foo"
    assert meta["limit"] == 2


def test_deployed_info_unknown_when_health_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(_common.httpx, "get", _boom)

    assert deployed_info("http://localhost:8000") == {
        "deployed_version": "unknown",
        "deployed_commit": "unknown",
    }


def test_prompt_versions_reads_prompts_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_get(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"status": "ok", "prompts": {"analyze-single-pass-system": 3}},
        )

    monkeypatch.setattr(_common.httpx, "get", _fake_get)

    assert prompt_versions("http://localhost:8000") == {"analyze-single-pass-system": 3}


def test_prompt_versions_empty_when_health_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(_common.httpx, "get", _boom)

    assert prompt_versions("http://localhost:8000") == {}


def test_prompt_versions_empty_when_health_predates_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_get(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(
            200, request=httpx.Request("GET", url), json={"status": "ok"}
        )

    monkeypatch.setattr(_common.httpx, "get", _fake_get)

    assert prompt_versions("http://localhost:8000") == {}
