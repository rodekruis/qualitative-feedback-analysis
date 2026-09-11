"""Score ``POST /v1/assign-codes`` against the Langfuse ``assign-codes/ukrain`` dataset.

Fires one real HTTP request per dataset item at a running QFA backend (the
same production vector ``scripts/stress_analyze.py`` uses for
``/v1/analyze``: real HTTP, real auth, real serialization) and records
per-level correctness as Langfuse scores on a linked Dataset Run. Report
only: this never raises to signal a low score, it only records what it
measured.

A coding level counts as correct when the API's top-ranked assigned code at
that level is one of the accepted names in the dataset item's
``expected_output`` for that level.

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
REQUEST_TIMEOUT_SECONDS = 180.0


async def run_assign_codes(*, item: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Call ``/v1/assign-codes`` for one dataset item; return ``assigned_codes``.

    A per-call client keeps this independent of whatever event loop the
    Langfuse SDK runs concurrent items on, at the cost of one TCP
    connection per item rather than a pooled one — acceptable for a
    report-only, manually-triggered evaluation run.
    """
    base_url = os.environ.get("QFA_API_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ["QFA_DEV_API_KEY"]
    body = {
        "coding_levels": item.input["hierarchy"]["coding_levels"],
        "feedback_record": {"content": item.input["content"], "id": str(item.id)},
        "max_codes": MAX_CODES,
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


def _level_evaluation(
    level: int, output: list[dict[str, Any]] | None, expected_output: dict[str, Any]
) -> Evaluation:
    """Score one coding level: correct if the top-ranked code is an accepted name."""
    expected = {str(name).strip() for name in expected_output.get(f"level_{level}", [])}
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
) -> Evaluation:
    """Was the top-ranked level-1 code one of the accepted names."""
    return _level_evaluation(1, output, expected_output)


def level_2_correct(
    *,
    output: list[dict[str, Any]] | None,
    expected_output: dict[str, Any],
    **kwargs: Any,
) -> Evaluation:
    """Was the top-ranked level-2 code one of the accepted names."""
    return _level_evaluation(2, output, expected_output)


def level_3_correct(
    *,
    output: list[dict[str, Any]] | None,
    expected_output: dict[str, Any],
    **kwargs: Any,
) -> Evaluation:
    """Was the top-ranked level-3 code one of the accepted names."""
    return _level_evaluation(3, output, expected_output)


def main() -> None:
    """Run the experiment against the full dataset and print a summary."""
    langfuse = get_client()
    dataset = langfuse.get_dataset(DATASET_NAME)
    result = dataset.run_experiment(
        name="assign-codes eval",
        description="Per-level accuracy of POST /v1/assign-codes against ground truth.",
        task=run_assign_codes,
        evaluators=[level_1_correct, level_2_correct, level_3_correct],
    )
    print(result.format())


if __name__ == "__main__":
    main()
