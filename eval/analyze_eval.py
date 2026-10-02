r"""Score ``POST /v1/analyze-bulk`` against a Langfuse analyze case set.

Each case is one prompt sent over the pool records in send order, with a
computed answer key as its ``expected_output`` (see
``make_analyze_cases.py``). This script sends the call, maps the response
to an outcome, and records both the service's own judge scores and the
reference scores computed against the key as one Langfuse Dataset Run.
It also saves the raw answers, so a changed scorer can be tried on a
finished run without paying for a new one.

It never fails on a low score, and never gates anything — it only
reports. Results are on synthetic data.

Prerequisites
-------------
- ``QFA_DEV_API_KEY``, or ``AUTH_API_KEYS`` with ``QFA_API_BASE_URL`` for
  a local server — see ``evaluate_sensitivity.py``.
- ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST`` —
  the Langfuse project holding the case set.

Run::

    uv run python eval/analyze_eval.py --dataset analyze/cases-frequent-en-v1 --smoke-limit 1
    uv run python eval/analyze_eval.py --dataset analyze/cases-frequent-en-v1
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langfuse import Evaluation, get_client
from langfuse.api import NotFoundError

from _common import load_env, prompt_versions, resolve_config, run_metadata
from analyze_scorers import judge_scores, score_answer

EXPERIMENT_NAME = "analyze-bulk"

# Every prompt name the analyze flows can tag a call with (#398). Recorded
# unconditionally rather than per item: a dataset mixes single_pass and
# hierarchical items (see ``item.input["mode"]``), and a run's metadata is
# one dict for the whole experiment, not one per item.
ANALYZE_PROMPTS = (
    "analyze-single-pass-system",
    "analyze-hierarchical-map-system",
    "analyze-hierarchical-reduce-system",
    "analyze-judge",
)

# One analyze call can run for minutes, and the dev backend has a small
# Postgres pool — 5 (the eval-wide default) would exhaust it. The task
# must stay async: the Langfuse SDK calls a sync task inside its own
# event loop, so a sync task would run one case at a time regardless of
# this setting.
MAX_CONCURRENCY = 2

# At or above the route's own 1,200-second deadline, so the service's own
# error — not a client-side timeout — is what the harness sees.
REQUEST_TIMEOUT_SECONDS = 1300.0

# Server-side trace ingestion lags a flush by up to ~30 seconds.
JUDGE_TRACE_TIMEOUT_SECONDS = 60.0
JUDGE_POLL_INTERVAL_SECONDS = 2.0
JUDGE_OBSERVATION_NAME = "analyze:judge"

DATASET_METADATA_KEYS = (
    "records_dataset",
    "records_version",
    "prompt_dataset",
)

# Where the raw answers of a run are saved, so a scorer change can be
# tried on them again without paying for a new run. Git ignores it.
ANSWERS_DIR = Path(".corpus_work/analyze-runs")

# Neither the service nor the analyst prompts ask the model to name the
# records behind a finding, so an answer cites record ids only by chance:
# one smoke answer over 111 records cited none, which scores every
# reference score 0 however good the answer is. The harness therefore
# asks. It names no theme, need or group, so it cannot tell the model
# what to find, and it gives no example id, so it cannot seed a citation.
CITE_RECORDS_INSTRUCTION = (
    "\n\nFor each point you report, list the ids of the feedback records it "
    "is based on, written exactly as the id attribute of those records."
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate /v1/analyze-bulk against a Langfuse case set.",
        epilog=(
            "Example:\n"
            "  uv run python eval/analyze_eval.py "
            "--dataset analyze/cases-frequent-en-v1 --smoke-limit 1"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Name of the Langfuse case-set dataset, e.g. analyze/cases-frequent-en-v1.",
    )
    parser.add_argument(
        "--smoke-limit",
        type=int,
        default=None,
        help="Run only the first N cases. Default: the full case set.",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="Optional Langfuse run name.",
    )
    return parser.parse_args()


def load_records(
    langfuse: Any, cases: Sequence[Any]
) -> dict[tuple[str, str], dict[str, Mapping[str, Any]]]:
    """Fetch each distinct ``records_dataset``/``records_version`` pair in ``cases`` once.

    Keyed exactly as a case's own metadata carries the pair, so
    ``_make_analyze`` looks a case's records up with no further parsing.
    Each dataset is fetched at its case-stored version, so the text sent
    matches the version the case's answer key was computed from.
    """
    fetched: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = {}
    for case in cases:
        dataset_name = case.metadata["records_dataset"]
        version = case.metadata["records_version"]
        key = (dataset_name, version)
        if key in fetched:
            continue
        items = langfuse.get_dataset(
            dataset_name, version=datetime.fromisoformat(version)
        ).items
        fetched[key] = {item.input["id"]: item.input for item in items}
    return fetched


def resolve_records(
    records: Mapping[str, Mapping[str, Any]], ids: Sequence[str]
) -> list[Mapping[str, Any]]:
    """``records[id]`` for each of ``ids``, in that order.

    Raises
    ------
    KeyError
        ``ids`` names a record that ``records`` does not hold. Raised
        with the unknown id rather than skipping it, so a stale or
        mistyped record id fails its case loudly instead of quietly
        shrinking the batch sent to the service.
    """
    return [records[record_id] for record_id in ids]


def build_request_body(
    prompt: str,
    mode: str,
    output_language: str,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The ``/v1/analyze-bulk`` request body: only the keys the service accepts.

    ``prompt`` is the analyst prompt plus ``CITE_RECORDS_INSTRUCTION``,
    because the reference scores read the record ids an answer cites.
    Each record sends only ``id``, ``content`` and ``metadata.created``.
    ``ApiFeedbackRecordMetadata`` rejects any other metadata key with a
    422 for the whole request, and the ``coding_level_*`` codes would
    hand the model the answer. The body never holds ``anonymize``
    (``ApiAnalyzeRequest`` has no such field), or ``espo_feedback_base_url``
    or ``url_id`` (together they turn cited ids into links).
    """
    return {
        "prompt": prompt + CITE_RECORDS_INSTRUCTION,
        "mode": mode,
        "output_language": output_language,
        "feedback_records": [
            {
                "id": record["id"],
                "content": record["content"],
                "metadata": {"created": record["metadata"]["created"]},
            }
            for record in records
        ],
    }


def outcome_of(status: int, body: Mapping[str, Any] | None) -> str:
    """The outcome name for one HTTP response.

    A 413 or a content-filter 422 are results the run continues past.
    Any other unsuccessful response — another 422, a 5xx, a timeout —
    is ``error``.
    """
    if status == 200:
        return "ok"
    if status == 413:
        return "payload_too_large"
    if (
        status == 422
        and body is not None
        and (body.get("error") or {}).get("code") == "content_policy_violation"
    ):
        return "content_filtered"
    return "error"


def _make_analyze(
    base_url: str,
    api_key: str,
    records_by_version: Mapping[tuple[str, str], Mapping[str, Mapping[str, Any]]],
) -> Callable[..., Any]:
    """Bind the harness's collaborators into a task matching Langfuse's ``TaskFunction`` shape."""

    async def analyze(*, item: Any, **kwargs: Any) -> dict[str, Any]:
        """Send one case's prompt and records, and map the response to an outcome."""
        records = records_by_version[
            (item.metadata["records_dataset"], item.metadata["records_version"])
        ]
        resolved = resolve_records(records, item.input["record_ids"])
        body = build_request_body(
            item.input["prompt"],
            item.input["mode"],
            item.input["output_language"],
            resolved,
        )

        try:
            async with httpx.AsyncClient(
                base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    "/v1/analyze-bulk",
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except httpx.HTTPError as exc:
            return {
                "outcome": "error",
                "status": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

        try:
            response_body = response.json()
        except ValueError:
            response_body = None

        outcome = outcome_of(response.status_code, response_body)
        if outcome != "ok" or response_body is None:
            return {
                "outcome": outcome,
                "status": response.status_code,
                "error": response_body,
            }

        return {
            **response_body,
            "outcome": "ok",
            "status": response.status_code,
            "request_id": response.headers.get("x-request-id"),
        }

    return analyze


def _judge_scores_evaluator(*, output: Any, **kwargs: Any) -> list[Evaluation]:
    return judge_scores(output)


def _outcome_evaluator(*, output: Any, **kwargs: Any) -> list[Evaluation]:
    return [
        Evaluation(name="outcome", value=output["outcome"], data_type="CATEGORICAL")
    ]


def _reference_scores_evaluator(
    *, output: Any, expected_output: Any, input: Any, **kwargs: Any
) -> list[Evaluation]:
    if output.get("outcome") != "ok":
        return []
    return score_answer(output["analysis"], expected_output, input["record_ids"])


def _find_ok_request_id(item_results: Sequence[Any]) -> str | None:
    """The ``request_id`` of the first ``ok`` item, or ``None`` if there is none."""
    for result in item_results:
        output = result.output
        if isinstance(output, Mapping) and output.get("outcome") == "ok":
            request_id = output.get("request_id")
            if request_id:
                return str(request_id)
    return None


def _read_judge_model(langfuse: Any, trace_id: str) -> str:
    """The judge model named in ``trace_id``'s ``analyze:judge`` observation.

    Retries for up to ``JUDGE_TRACE_TIMEOUT_SECONDS`` rather than failing
    on the first read, because ingestion lags a flush. Prints a warning
    and returns ``"unknown"`` if the trace or the judge observation never
    appears in that window.
    """
    deadline = time.monotonic() + JUDGE_TRACE_TIMEOUT_SECONDS
    while True:
        try:
            trace = langfuse.api.trace.get(trace_id)
        except NotFoundError:
            trace = None
        if trace is not None:
            for observation in trace.observations:
                if observation.name == JUDGE_OBSERVATION_NAME and observation.model:
                    return observation.model
        if time.monotonic() >= deadline:
            break
        time.sleep(JUDGE_POLL_INTERVAL_SECONDS)
    print(f"warning: no {JUDGE_OBSERVATION_NAME} observation found in trace {trace_id}")
    return "unknown"


def _case_label(item: Any) -> str:
    """The case's own id, e.g. ``P03-en``."""
    return f"{item.input['prompt_id']}-{item.metadata['language']}"


def save_answers(item_results: Sequence[Any], run_name: str) -> Path:
    """Write one JSON line per case to ``ANSWERS_DIR/<run name>.jsonl``, and return the path.

    Holds everything ``score_answer`` needs — the answer, the key and the
    ids that were sent — so a changed scorer can be tried on a finished
    run at no cost. ``faithfulness`` and ``uncertainty_explanation`` ride
    along because a review of a low-faithfulness case compares the records
    that explanation names with their labels, and the Langfuse UI is the
    only other place that holds them. A case that is not ``ok`` has a null
    ``analysis``.
    """
    ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    path = ANSWERS_DIR / f"{run_name}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for item_result in item_results:
            item = item_result.item
            output = (
                item_result.output if isinstance(item_result.output, Mapping) else {}
            )
            handle.write(
                json.dumps(
                    {
                        "case": _case_label(item),
                        "outcome": output.get("outcome"),
                        "analysis": output.get("analysis"),
                        "faithfulness": output.get("faithfulness"),
                        "uncertainty_explanation": output.get(
                            "uncertainty_explanation"
                        ),
                        "key": item.expected_output,
                        "record_ids": item.input["record_ids"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def _raw_counts_line(item_result: Any) -> str:
    """One line of raw counts for a case, e.g. ``P03-en urgent_ids_cited 6/6, ...``."""
    case_label = _case_label(item_result.item)
    parts = [
        f"{evaluation.name} {evaluation.comment.split(',', 1)[0]}"
        for evaluation in item_result.evaluations
        if evaluation.comment
    ]
    return f"{case_label} " + ", ".join(parts)


def main() -> None:
    """Run a case set against the service and record one Langfuse Dataset Run."""
    args = _parse_args()
    load_env()
    base_url, api_key = resolve_config()

    langfuse = get_client()
    cases_version = datetime.now(UTC)
    dataset = langfuse.get_dataset(args.dataset, version=cases_version)

    if args.smoke_limit is not None:
        if args.smoke_limit <= 0:
            raise SystemExit("--smoke-limit must be greater than 0.")
        # dataset.run_experiment() always runs dataset.items itself (there is
        # no way to pass it a shorter list), so the slice must be assigned
        # back rather than kept in a separate local variable.
        dataset.items = dataset.items[: args.smoke_limit]
    items = dataset.items

    records_by_version = load_records(langfuse, items)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_kind = "smoke" if args.smoke_limit else "full"
    dataset_label = args.dataset.rsplit("/", 1)[-1]
    if args.run_name:
        run_name = args.run_name
    elif args.smoke_limit:
        run_name = f"{dataset_label}-smoke-{args.smoke_limit}-{timestamp}"
    else:
        run_name = f"{dataset_label}-full-{timestamp}"

    dataset_keys = {key: items[0].metadata[key] for key in DATASET_METADATA_KEYS}
    _analyze_prompt_versions = prompt_versions(base_url)

    print(f"Dataset: {args.dataset}")
    print(f"Backend: {base_url}")
    print(f"Cases:   {len(items)}")
    print(f"Run:     {run_name}")
    print(
        f"Sending, up to {MAX_CONCURRENCY} at a time — one analyze call can take "
        "several minutes; nothing else prints until the whole run finishes."
    )
    print()

    result = dataset.run_experiment(
        name=EXPERIMENT_NAME,
        run_name=run_name,
        task=_make_analyze(base_url, api_key, records_by_version),
        evaluators=[
            _judge_scores_evaluator,
            _outcome_evaluator,
            _reference_scores_evaluator,
        ],
        max_concurrency=MAX_CONCURRENCY,
        metadata=run_metadata(
            base_url,
            dataset=args.dataset,
            endpoint="/v1/analyze-bulk",
            run_kind=run_kind,
            smoke_limit=args.smoke_limit,
            cases_version=cases_version.isoformat(),
            prompt_versions={
                name: _analyze_prompt_versions.get(name) for name in ANALYZE_PROMPTS
            },
            **dataset_keys,
        ),
    )

    print(result.format())
    print()
    for item_result in result.item_results:
        print(_raw_counts_line(item_result))

    print(f"\nAnswers: {save_answers(result.item_results, run_name)}")

    request_id = _find_ok_request_id(result.item_results)
    if request_id is not None:
        judge_model = _read_judge_model(langfuse, request_id.replace("-", ""))
    else:
        judge_model = "unknown"
        print("warning: no ok case to read the judge model from")
    langfuse.create_score(
        name="judge_model",
        value=judge_model,
        dataset_run_id=result.dataset_run_id,
        data_type="CATEGORICAL",
    )

    failed = len(items) - len(result.item_results)
    print(f"\n{failed} case(s) failed")
    if result.dataset_run_url:
        print(f"Langfuse: {result.dataset_run_url}")


if __name__ == "__main__":
    main()
