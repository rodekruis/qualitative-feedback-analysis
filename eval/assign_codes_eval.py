"""Score ``POST /v1/assign-codes`` against the Langfuse ``assign-codes/ukrain`` dataset.

Fires one real HTTP request per dataset item at a running QFA backend (the
same production vector ``scripts/stress_analyze.py`` uses for
``/v1/analyze``: real HTTP, real auth, real serialization) and records
per-level correctness as Langfuse scores on a linked Dataset Run. Report
only: this never raises to signal a low score, it only records what it
measured.

A coding level counts as correct when the API's top-ranked assigned code at
that level is one of the accepted names in the dataset item's
``expected_output`` for that level. A level with no ``expected_output`` key
is left unscored, not marked correct, since that means the record was never
labelled at that level, not that no code should apply.

Each run records the backend's deployed ``version`` and ``commit`` (both
read from ``GET /v1/health``), plus the eval script's own commit and
branch, as separate run-metadata fields, since a ``dev`` deploy can run
code the script was never checked out at (see ``_deployed_info``).

Prerequisites
-------------
- ``QFA_DEV_API_KEY`` — bearer token for ``QFA_API_BASE_URL`` (default
  ``https://qfa-dev-backend.azurewebsites.net``).
- ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST`` — the
  Langfuse project holding the ``assign-codes/ukrain`` dataset.

Run::

    uv run python eval/assign_codes_eval.py
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import dotenv
import httpx
from langfuse import Evaluation, get_client

REPO_ROOT = Path(__file__).resolve().parents[1]
dotenv.load_dotenv(REPO_ROOT / ".env")

DATASET_NAME = "assign-codes/ukrain"
DEFAULT_BASE_URL = "https://qfa-dev-backend.azurewebsites.net"
MAX_CODES = 10
# Sent explicitly, rather than left out of the request, so a future change
# to the server's default cannot silently change what an eval run measures.
# ``None`` means every candidate the model proposes is kept, regardless of
# confidence.
CONFIDENCE_THRESHOLD: float | None = None
REQUEST_TIMEOUT_SECONDS = 180.0
# run_experiment defaults to 50 concurrent items, which exhausts the dev
# backend's small Postgres connection pool (usage tracking then fails, and
# every request slows down). Keep this well under that pool's capacity.
MAX_CONCURRENCY = 5


def _resolve_config() -> tuple[str, str]:
    """The backend base URL and bearer token for this run.

    Exits with a clear message when ``QFA_DEV_API_KEY`` is missing, instead
    of letting every dataset item fail separately after the run has started.
    """
    base_url = os.environ.get("QFA_API_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("QFA_DEV_API_KEY")
    if not api_key:
        raise SystemExit(
            "QFA_DEV_API_KEY is not set. See eval/README.md for prerequisites."
        )
    return base_url, api_key


def _deployed_info(base_url: str) -> dict[str, str]:
    """The package version and git commit that ``base_url`` reports at ``/v1/health``.

    Both default to ``"unknown"`` when the health check fails, so a run
    still proceeds against a backend that is reachable for
    ``/v1/assign-codes`` but does not answer ``/v1/health`` for some other
    reason. ``commit`` is the field that actually identifies the deployed
    code: ``version`` only changes on a semantic-release bump, so an
    ephemeral ``dev`` deploy (``build-from-commit.yaml``) can carry no
    version bump at all and would otherwise be indistinguishable from
    whatever was deployed before it.
    """
    try:
        response = httpx.get(f"{base_url}/v1/health", timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        return {
            "deployed_version": str(data["version"]),
            "deployed_commit": str(data["commit"]),
        }
    except (httpx.HTTPError, KeyError) as e:
        print(f"warning: could not read {base_url}/v1/health ({e})")
        return {"deployed_version": "unknown", "deployed_commit": "unknown"}


def _make_run_assign_codes(
    base_url: str, api_key: str
) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
    """Bind ``base_url``/``api_key`` into a task matching Langfuse's ``TaskFunction`` shape."""

    async def run_assign_codes(*, item: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Call ``/v1/assign-codes`` for one dataset item; return ``assigned_codes``.

        A per-call client keeps this independent of whatever event loop the
        Langfuse SDK runs concurrent items on, at the cost of one TCP
        connection per item rather than a pooled one — acceptable for a
        report-only, manually-triggered evaluation run.
        """
        body = {
            "coding_levels": item.input["hierarchy"]["coding_levels"],
            "feedback_record": {"content": item.input["content"], "id": str(item.id)},
            "max_codes": MAX_CODES,
            "confidence_threshold": CONFIDENCE_THRESHOLD,
        }
        async with httpx.AsyncClient(
            base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                "/v1/assign-codes",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        response.raise_for_status()
        return response.json()["assigned_codes"]

    return run_assign_codes


def _level_evaluation(
    level: int, output: list[dict[str, Any]] | None, expected_output: dict[str, Any]
) -> Evaluation | list[Evaluation]:
    """Score one coding level: correct if the top-ranked code is an accepted name.

    Returns an empty list when ``expected_output`` has no key for this
    level, so an unlabelled level is left unscored rather than counted as
    "no code expected". Returns a score of 0.0, never 1.0, when ``output``
    is ``None``, so a failed API call cannot look like a correct prediction.
    """
    key = f"level_{level}"
    if key not in expected_output:
        return []
    expected = {str(name).strip() for name in expected_output[key]}
    if output is None:
        return Evaluation(
            name=f"level_{level}_correct",
            value=0.0,
            comment="the API call failed; there is no prediction to score",
        )
    top = output[0] if output else None
    predicted = top.get(f"coding_level_{level}_name") if top else None
    predicted = predicted.strip() if predicted else None
    correct = predicted in expected if predicted else not expected
    return Evaluation(
        name=f"level_{level}_correct",
        value=1.0 if correct else 0.0,
        comment=f"predicted={predicted!r} expected={sorted(expected)!r}",
    )


def level_1_correct(
    *,
    output: list[dict[str, Any]] | None,
    expected_output: dict[str, Any],
    **kwargs: Any,
) -> Evaluation | list[Evaluation]:
    """Was the top-ranked level-1 code one of the accepted names."""
    return _level_evaluation(1, output, expected_output)


def level_2_correct(
    *,
    output: list[dict[str, Any]] | None,
    expected_output: dict[str, Any],
    **kwargs: Any,
) -> Evaluation | list[Evaluation]:
    """Was the top-ranked level-2 code one of the accepted names."""
    return _level_evaluation(2, output, expected_output)


def level_3_correct(
    *,
    output: list[dict[str, Any]] | None,
    expected_output: dict[str, Any],
    **kwargs: Any,
) -> Evaluation | list[Evaluation]:
    """Was the top-ranked level-3 code one of the accepted names."""
    return _level_evaluation(3, output, expected_output)


def _git_output(*args: str) -> str:
    """Run a git command in the repo; empty string on any failure."""
    # Fixed executable, fixed-shape args from this file only — not user input.
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_metadata() -> dict[str, str]:
    """The commit SHA and branch that this eval *script* was checked out at.

    This names the code that sent the requests, not the code that answered
    them — the backend under test may be running something else entirely
    (see ``_deployed_info``). CI sets ``GIT_SHA``/``GIT_REF_NAME`` (from
    the GitHub Actions context, which knows the ref even in a detached-HEAD
    checkout); a local run falls back to asking git directly.
    """
    sha = os.environ.get("GIT_SHA") or _git_output("rev-parse", "HEAD") or "unknown"
    branch = (
        os.environ.get("GIT_REF_NAME")
        or _git_output("rev-parse", "--abbrev-ref", "HEAD")
        or "unknown"
    )
    return {"eval_script_sha": sha, "git_branch": branch}


def main() -> None:
    """Run the experiment against the full dataset and print a summary."""
    base_url, api_key = _resolve_config()
    deployed = _deployed_info(base_url)
    git_meta = _git_metadata()
    run_metadata = {
        **git_meta,
        **deployed,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    }

    langfuse = get_client()
    dataset = langfuse.get_dataset(DATASET_NAME)
    result = dataset.run_experiment(
        name="assign-codes eval",
        run_name=(
            f"assign-codes eval (deployed {deployed['deployed_version']} @ "
            f"{deployed['deployed_commit'][:7]}, script {git_meta['git_branch']} @ "
            f"{git_meta['eval_script_sha'][:7]})"
        ),
        description="Per-level accuracy of POST /v1/assign-codes against ground truth.",
        task=_make_run_assign_codes(base_url, api_key),
        evaluators=[level_1_correct, level_2_correct, level_3_correct],
        max_concurrency=MAX_CONCURRENCY,
        metadata=run_metadata,
    )
    print(result.format())


if __name__ == "__main__":
    main()
