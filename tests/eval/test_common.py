"""Tests for the shared helpers in ``eval/_common.py``.

``eval/`` is not a package. Load the module by file path so the tests run
regardless of where pytest is invoked from (mirrors
``tests/scripts/test_stress_analyze.py``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langfuse.api import NotFoundError

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "eval" / "_common.py"
_spec = importlib.util.spec_from_file_location("_common", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_common = importlib.util.module_from_spec(_spec)
sys.modules["_common"] = _common
_spec.loader.exec_module(_common)

DEFAULT_BASE_URL = _common.DEFAULT_BASE_URL
contains_term = _common.contains_term
deployed_info = _common.deployed_info
git_metadata = _common.git_metadata
resolve_api_key = _common.resolve_api_key
resolve_config = _common.resolve_config
run_metadata = _common.run_metadata
upload_items = _common.upload_items
item_id = _common._item_id

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


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        pytest.param("will", "ill", False, id="ill-not-inside-will"),
        pytest.param("said", "aid", False, id="aid-not-inside-said"),
        pytest.param("LGBTQI+", "lgbtqi", True, id="lgbtqi-plus-folds-to-lgbtqi"),
    ],
)
def test_contains_term_matches_whole_words_only(
    text: str, term: str, expected: bool
) -> None:
    assert contains_term(text, term) is expected


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


@dataclass
class _FakeItem:
    """A minimal stand-in for a Langfuse ``DatasetItem``."""

    id: str
    input: Any
    expected_output: Any = None
    metadata: Any = None


class FakeLangfuseClient:
    """A duck-typed double for the Langfuse SDK client, for :func:`upload_items`.

    ``datasets`` seeds existing dataset items, keyed by dataset name (used
    here as the fake's dataset id too).
    """

    def __init__(
        self,
        datasets: dict[str, list[_FakeItem]] | None = None,
    ) -> None:
        self._datasets = datasets or {}
        self.created_datasets: list[str] = []
        self.written_items: list[dict[str, Any]] = []

    def get_dataset(self, name: str) -> SimpleNamespace:
        if name not in self._datasets:
            raise NotFoundError(body={"message": "not found"})
        return SimpleNamespace(id=name, items=self._datasets[name])

    def create_dataset(self, *, name: str, **_kwargs: Any) -> None:
        self.created_datasets.append(name)
        self._datasets.setdefault(name, [])

    def create_dataset_item(
        self,
        *,
        dataset_name: str,
        id: str,
        input: Any,
        expected_output: Any,
        metadata: Any,
    ) -> None:
        self.written_items.append({"dataset_name": dataset_name, "id": id})
        items = self._datasets.setdefault(dataset_name, [])
        items[:] = [i for i in items if i.id != id]
        items.append(
            _FakeItem(
                id=id, input=input, expected_output=expected_output, metadata=metadata
            )
        )


def test_upload_items_creates_the_dataset_when_it_does_not_exist() -> None:
    client = FakeLangfuseClient()

    upload_items(client, "analyze/prompts-v1", [{"id": "P01-en", "input": "hi"}])

    assert client.created_datasets == ["analyze/prompts-v1"]


def test_upload_items_builds_an_id_with_no_slash_or_hyphen() -> None:
    """Langfuse's item page 404s on a raw "/" or "-" in the id (langfuse/langfuse#17259)."""
    full_id = item_id("feedback/records-en-v1", "en-0001")

    assert full_id == "feedback_records_en_v1:en_0001"
    assert "/" not in full_id
    assert "-" not in full_id


def test_upload_items_creates_a_new_item_with_a_url_safe_id() -> None:
    client = FakeLangfuseClient(datasets={"analyze/prompts-v1": []})

    report = upload_items(
        client,
        "analyze/prompts-v1",
        [{"id": "P01-en", "input": "hi", "metadata": {"family": "themes"}}],
    )

    full_id = item_id("analyze/prompts-v1", "P01-en")
    assert report.created == [full_id]
    assert client.written_items == [
        {"dataset_name": "analyze/prompts-v1", "id": full_id}
    ]


def test_upload_items_skips_an_item_with_unchanged_content() -> None:
    full_id = item_id("analyze/prompts-v1", "P01-en")
    existing = _FakeItem(
        id=full_id,
        input="hi",
        expected_output=None,
        metadata={"family": "themes"},
    )
    client = FakeLangfuseClient(datasets={"analyze/prompts-v1": [existing]})

    report = upload_items(
        client,
        "analyze/prompts-v1",
        [{"id": "P01-en", "input": "hi", "metadata": {"family": "themes"}}],
    )

    assert report.unchanged == [full_id]
    assert report.created == []
    assert client.written_items == []


def test_upload_items_updates_a_changed_item() -> None:
    full_id = item_id("analyze/prompts-v1", "P01-en")
    existing = _FakeItem(id=full_id, input="old text")
    client = FakeLangfuseClient(datasets={"analyze/prompts-v1": [existing]})

    report = upload_items(
        client,
        "analyze/prompts-v1",
        [{"id": "P01-en", "input": "new text"}],
    )

    assert report.updated == [full_id]
    assert client.written_items == [
        {"dataset_name": "analyze/prompts-v1", "id": full_id}
    ]


def test_upload_items_prints_and_reports_an_id_missing_from_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale_id = item_id("analyze/prompts-v1", "P99-en")
    stale = _FakeItem(id=stale_id, input="orphaned")
    client = FakeLangfuseClient(datasets={"analyze/prompts-v1": [stale]})

    report = upload_items(
        client, "analyze/prompts-v1", [{"id": "P01-en", "input": "hi"}]
    )

    assert report.extra == [stale_id]
    assert stale_id in capsys.readouterr().out
    # Deletes nothing: the stale item is still in the fake store.
    assert any(i.id == stale_id for i in client._datasets["analyze/prompts-v1"])


def test_upload_items_dry_run_writes_nothing() -> None:
    client = FakeLangfuseClient()

    report = upload_items(
        client, "analyze/prompts-v1", [{"id": "P01-en", "input": "hi"}], dry_run=True
    )

    assert report.created == [item_id("analyze/prompts-v1", "P01-en")]
    assert client.created_datasets == []
    assert client.written_items == []
