"""Shared helpers for ``eval/`` scripts.

``eval/`` is not a package and must not become one: ``uv run python eval/x.py``
puts ``eval/`` on ``sys.path[0]``, so scripts import this as
``from _common import ...``. That keeps the workflow ``run:`` lines unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]

MAX_CONCURRENCY = 5
DEFAULT_BASE_URL = "https://qfa-dev-backend.azurewebsites.net"
# Health check only. Per-request timeouts stay in each script — they measure
# endpoint latency, not shared policy.
HEALTH_TIMEOUT_SECONDS = 180.0


def load_env() -> None:
    """Load the repo-root ``.env`` into ``os.environ``.

    A call, not an import side effect: importing this module from a test
    must not pull a real untracked ``.env`` into the process.
    """
    load_dotenv(_REPO_ROOT / ".env")


def resolve_api_key() -> str:
    """The API key to call the QFA service with.

    ``QFA_DEV_API_KEY`` first — the convention ``assign_codes_eval.py`` and the
    **evaluate** CI workflow use, so every script works unchanged wherever that
    one does. Falling back to ``AUTH_API_KEYS`` means a local run needs nothing
    beyond the keys the local server is already configured with.

    The fallback parses that JSON by hand rather than going through
    ``AuthSettings``: :class:`~qfa.domain.models.TenantApiKey` blanks the
    plaintext ``key`` as soon as it has hashed it, so the settings object can
    only ever answer with ``None``. Any entry authenticates — eval scripts
    need no superuser flag.
    """
    if key := os.environ.get("QFA_DEV_API_KEY"):
        return key
    raw = os.environ.get("AUTH_API_KEYS")
    if not raw:
        raise SystemExit(
            "No API key: set QFA_DEV_API_KEY, or AUTH_API_KEYS to the same "
            "JSON the server consumes."
        )
    for entry in json.loads(raw):
        if key := entry.get("key"):
            return str(key)
    raise SystemExit(
        "No usable API key in AUTH_API_KEYS: every entry stores only a "
        "'hashed_key'. Set QFA_DEV_API_KEY instead, or give one entry a "
        "plaintext 'key'."
    )


def resolve_config() -> tuple[str, str]:
    """The backend base URL and bearer token for this run.

    Defaults to the dev backend. A trailing slash is stripped so callers can
    concatenate paths. Exits when neither ``QFA_DEV_API_KEY`` nor a plaintext
    ``AUTH_API_KEYS`` entry is set, instead of letting every dataset item fail
    after the run has started.
    """
    base_url = os.environ.get("QFA_API_BASE_URL") or DEFAULT_BASE_URL
    return base_url.rstrip("/"), resolve_api_key()


def deployed_info(base_url: str) -> dict[str, str]:
    """The package version and git commit that ``base_url`` reports at ``/v1/health``.

    Both default to ``"unknown"`` when the health check fails, so a run still
    proceeds against a backend that is reachable for the scored endpoint but
    does not answer ``/v1/health``. ``commit`` is the field that actually
    identifies the deployed code: ``version`` only changes on a semantic-release
    bump, so an ephemeral ``dev`` deploy (``build-from-commit.yaml``) can carry
    no version bump at all and would otherwise be indistinguishable from
    whatever was deployed before it.
    """
    try:
        response = httpx.get(f"{base_url}/v1/health", timeout=HEALTH_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        return {
            "deployed_version": str(data["version"]),
            "deployed_commit": str(data["commit"]),
        }
    except (httpx.HTTPError, KeyError) as e:
        print(f"warning: could not read {base_url}/v1/health ({e})")
        return {"deployed_version": "unknown", "deployed_commit": "unknown"}


def _git_output(*args: str) -> str:
    """Run a git command in the repo; empty string on any failure."""
    # Fixed executable, fixed-shape args from this file only — not user input.
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_metadata() -> dict[str, str]:
    """The commit SHA and branch that this eval *script* was checked out at.

    This names the code that sent the requests, not the code that answered
    them — the backend under test may be running something else entirely
    (see ``deployed_info``). CI sets ``GIT_SHA``/``GIT_REF_NAME`` (from the
    GitHub Actions context, which knows the ref even in a detached-HEAD
    checkout); a local run falls back to asking git directly.
    """
    sha = os.environ.get("GIT_SHA") or _git_output("rev-parse", "HEAD") or "unknown"
    branch = (
        os.environ.get("GIT_REF_NAME")
        or _git_output("rev-parse", "--abbrev-ref", "HEAD")
        or "unknown"
    )
    return {"eval_script_sha": sha, "git_branch": branch}


def run_metadata(base_url: str, **extra: Any) -> dict[str, Any]:
    """The four contract keys every eval run records, plus caller extras.

    ``eval_script_sha`` and ``git_branch`` name the script that sent the
    requests. ``deployed_version`` and ``deployed_commit`` name the backend
    that answered them. Run *names* stay per-script — they are how existing
    Langfuse views filter, and unifying them would break those views.
    """
    return {**git_metadata(), **deployed_info(base_url), **extra}
