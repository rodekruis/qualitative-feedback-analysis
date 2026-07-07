r"""Drive ``POST /v1/analyze`` with samples from the labelled corpus.

Helper script (run manually, not in CI). Two use cases:

1. **Quality smoke test** — fire one request with ``--concurrency 1``
   and inspect the response. Same surface as a real client; lets you
   confirm hierarchical mode works end-to-end against the live LLM.
2. **Stress test** — fire ``--concurrency N`` parallel requests against
   ``/v1/analyze`` to measure latency distribution and failure modes
   under load. This is the production usage vector — anything else
   (in-process orchestrator calls, fake LLMs) bypasses the auth,
   request validation, and serialization that real callers hit.

The interactive **quality-analysis notebook** in
``notebooks/analyze_corpus.ipynb`` covers a different need: it imports
:func:`qfa.api.composition.build_orchestrator` and calls
``analyze_hierarchical`` in-process so you can step through chunking,
swap settings per-cell, and plot ``coding_trends`` without a server.
Use the notebook to study behaviour; use this script to measure
behaviour under realistic load.

Run::

    uv run python scripts/stress_analyze.py \\
        --limit 100 --seed 42 \\
        --concurrency 1 \\
        --base-url http://localhost:8000

The corpus is loaded from ``fixtures/analyze_corpus.yaml`` by default
and sampled with a deterministic seed so reruns are comparable.

The first ``AUTH_API_KEYS`` entry (as the running server would read
it) is used for Bearer auth; override with ``--api-key``. Per-request
errors are captured rather than raised so a slow or failed call does
not abort the batch. Raw results land in
``.corpus_work/stress_<UTC-ts>.jsonl`` for follow-up analysis.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger("stress_analyze")

# Repo paths — the script lives at ``scripts/stress_analyze.py`` so the
# repo root is one up from the script directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "fixtures" / "analyze_corpus.yaml"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".corpus_work"

DEFAULT_PROMPT = (
    "Identify the main themes, sentiments, and any trending concerns "
    "in this feedback corpus. Call out signals that vary across the "
    "time period covered by the records."
)

# Hierarchical analysis can take a while for large samples (each chunk
# is a separate LLM call). Give the client a generous timeout above
# whatever the server enforces; failure modes worth observing are
# server-side rejects, not client-side cut-offs.
DEFAULT_TIMEOUT_S = 600.0

# ApiFeedbackRecordMetadata (qfa.api.schemas) accepts only these fields
# (extra="forbid"); the corpus fixture carries extra benchmark-only
# metadata (dataset, region, country, language, source, year, sensitive,
# codes, sentence_count, ...), so it must be projected down before hitting
# the real endpoint or every record gets rejected with a 422.
KNOWN_METADATA_FIELDS = (
    "created",
    "coding_level_1",
    "coding_level_2",
    "coding_level_3",
)


# --- Loading & sampling -------------------------------------------------------


def load_sample(
    yaml_path: Path,
    limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Read ``yaml_path`` and return ``limit`` records sampled with ``seed``.

    Parameters
    ----------
    yaml_path : Path
        YAML file holding a list of ``{id,  content, metadata}`` records.
    limit : int
        Number of records to keep. ``limit >= len(corpus)`` returns the
        full corpus (still seeded-shuffled, so order is deterministic).
    seed : int
        Seed for :class:`random.Random` — same seed → same subset.

    Returns
    -------
    list[dict]
        Records ready to be embedded into a ``/v1/analyze`` request body.
    """
    with yaml_path.open("r", encoding="utf-8") as fp:
        corpus = yaml.safe_load(fp)
    if not isinstance(corpus, list):
        raise ValueError(
            f"{yaml_path} did not contain a list of records "
            f"(got {type(corpus).__name__})"
        )

    rng = random.Random(seed)  # noqa: S311  (non-crypto sampling, intentional)
    if limit >= len(corpus):
        sample = list(corpus)
        rng.shuffle(sample)
        return sample
    return rng.sample(corpus, limit)


def _known_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Project a corpus record's metadata down to what the API will accept."""
    return {k: v for k, v in metadata.items() if k in KNOWN_METADATA_FIELDS}


def build_request(
    records: list[dict[str, Any]],
    prompt: str,
    *,
    mode: str = "hierarchical",
    anonymize: bool = True,
    period: str | None = None,
) -> dict[str, Any]:
    """Shape a ``/v1/analyze`` request body from sampled records.

    The API expects ``feedback_records`` (not ``documents``); the fixture
    YAML stores them as ``{id, content, metadata}``, which already matches
    the shape, but each record's ``metadata`` is projected down to the
    fields ``ApiFeedbackRecordMetadata`` actually accepts (``extra="forbid"``)
    — the fixture's benchmark-only metadata (``dataset``, ``region``,
    ``theme``, ...) would otherwise 422 the whole request.
    """
    projected_records = [
        {**record, "metadata": _known_metadata(record.get("metadata", {}))}
        for record in records
    ]
    body: dict[str, Any] = {
        "feedback_records": projected_records,
        "prompt": prompt,
        "mode": mode,
        "anonymize": anonymize,
    }
    if period is not None:
        body["period"] = period
    return body


# --- Per-request execution ----------------------------------------------------


@dataclass
class RunResult:
    """Outcome of one ``/v1/analyze`` call.

    On success ``status`` is the HTTP code and ``response`` is the
    decoded JSON body. On failure ``error`` carries a short string
    describing what went wrong; ``response`` is ``None``. ``latency_s``
    is always populated — even failures have a wall-clock cost worth
    reporting.
    """

    status: int | None
    latency_s: float
    response: dict[str, Any] | None = None
    error: str | None = None
    request_id: str | None = None

    def to_jsonl(self) -> str:
        """Serialise to one JSON Lines record for ``.corpus_work/`` output."""
        return json.dumps(
            {
                "status": self.status,
                "latency_s": round(self.latency_s, 3),
                "request_id": self.request_id,
                "error": self.error,
                "response": self.response,
            },
            ensure_ascii=False,
        )


async def _post_once(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    api_key: str,
) -> RunResult:
    """Issue one ``POST /v1/analyze`` and capture the outcome."""
    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.perf_counter()
    try:
        response = await client.post("/v1/analyze-bulk", json=body, headers=headers)
    except httpx.HTTPError as exc:
        return RunResult(
            status=None,
            latency_s=time.perf_counter() - start,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency = time.perf_counter() - start
    request_id = response.headers.get("x-request-id")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.is_success:
        return RunResult(
            status=response.status_code,
            latency_s=latency,
            response=payload,
            request_id=request_id,
        )
    # Non-2xx is a captured failure, not a raised exception — we want
    # the stress run to keep going and the summary to surface the
    # status-code distribution.
    return RunResult(
        status=response.status_code,
        latency_s=latency,
        response=payload,
        error=f"HTTP {response.status_code}",
        request_id=request_id,
    )


async def run_batch(
    records: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    concurrency: int = 1,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    prompt: str = DEFAULT_PROMPT,
    mode: str = "hierarchical",
    anonymize: bool = True,
    period: str | None = None,
    total_calls: int | None = None,
) -> list[RunResult]:
    """Fire ``total_calls`` (default = ``concurrency``) requests at the API.

    Every request posts the *same* body — that mirrors the realistic
    case where one analyst's corpus is being analysed; concurrency
    measures the server's ability to handle parallel work, not its
    ability to cache responses.

    Parameters
    ----------
    records : list[dict]
        Records to embed into the body. The same list is reused for
        every request.
    base_url : str
        e.g. ``http://localhost:8000``.
    api_key : str
        Bearer token sent on every request.
    concurrency : int
        Max in-flight requests at once.
    timeout_s : float
        Per-request httpx timeout (read + write + pool).
    prompt, mode, anonymize, period : forwarded to :func:`build_request`.
    total_calls : int | None
        How many requests to fire in total. Defaults to ``concurrency``
        — i.e. fan out N parallel calls once. Set higher to sustain
        throughput across multiple rounds.

    Returns
    -------
    list[RunResult]
        One result per request, in completion order is not guaranteed;
        we return them in *submission* order so callers can match up
        with their inputs.
    """
    if total_calls is None:
        total_calls = concurrency

    body = build_request(
        records,
        prompt,
        mode=mode,
        anonymize=anonymize,
        period=period,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_s),
    ) as client:

        async def _bounded() -> RunResult:
            async with semaphore:
                return await _post_once(client, body, api_key)

        tasks = [asyncio.create_task(_bounded()) for _ in range(total_calls)]
        results: list[RunResult] = []
        for coro in tasks:
            try:
                results.append(await coro)
            except Exception as exc:  # defensive — should be caught inside _post_once
                results.append(
                    RunResult(
                        status=None,
                        latency_s=0.0,
                        error=f"unhandled: {type(exc).__name__}: {exc}",
                    )
                )
    return results


# --- Summarisation ------------------------------------------------------------


@dataclass
class BatchSummary:
    """Aggregate statistics over a list of :class:`RunResult`."""

    total: int
    successes: int
    failures: int
    status_counts: Counter[int | None] = field(default_factory=Counter)
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    latency_p99: float = 0.0
    latency_min: float = 0.0
    latency_max: float = 0.0


def summarize(results: list[RunResult]) -> BatchSummary:
    """Reduce per-request results to a printable summary.

    Latency percentiles are computed over *all* results (successes
    and failures alike), because in a real client a failure that came
    back fast is qualitatively different from one that timed out.
    """
    statuses: Counter[int | None] = Counter(r.status for r in results)
    latencies = sorted(r.latency_s for r in results)
    successes = sum(1 for r in results if r.error is None)

    def _quantile(p: float) -> float:
        if not latencies:
            return 0.0
        # Nearest-rank percentile is good enough for an N-of-tens
        # stress run; statistics.quantiles is overkill here.
        idx = max(0, min(len(latencies) - 1, round(p * (len(latencies) - 1))))
        return latencies[idx]

    return BatchSummary(
        total=len(results),
        successes=successes,
        failures=len(results) - successes,
        status_counts=statuses,
        latency_p50=_quantile(0.50),
        latency_p95=_quantile(0.95),
        latency_p99=_quantile(0.99),
        latency_min=latencies[0] if latencies else 0.0,
        latency_max=latencies[-1] if latencies else 0.0,
    )


def format_summary(summary: BatchSummary) -> str:
    """Render a one-screen text summary for stdout."""
    lines = [
        f"Requests:   {summary.total} total — "
        f"{summary.successes} ok / {summary.failures} failed",
        "Status:     "
        + ", ".join(
            f"{code if code is not None else 'no-response'}={count}"
            for code, count in sorted(
                summary.status_counts.items(),
                key=lambda kv: (kv[0] is None, kv[0] or 0),
            )
        ),
        f"Latency s:  p50={summary.latency_p50:.2f}  "
        f"p95={summary.latency_p95:.2f}  p99={summary.latency_p99:.2f}  "
        f"min={summary.latency_min:.2f}  max={summary.latency_max:.2f}",
    ]
    return "\n".join(lines)


def write_jsonl(results: list[RunResult], output_dir: Path) -> Path:
    """Persist raw results for follow-up analysis."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = output_dir / f"stress_{ts}.jsonl"
    with out_path.open("w", encoding="utf-8") as fp:
        for r in results:
            fp.write(r.to_jsonl())
            fp.write("\n")
    return out_path


# --- Auth ---------------------------------------------------------------------


def resolve_api_key(explicit: str | None) -> str:
    """Return the API key, either from ``--api-key`` or ``AUTH_API_KEYS``.

    Using ``AuthSettings`` rather than reading ``AUTH_API_KEYS`` by
    hand keeps this script consistent with how the server itself
    loads keys: same JSON shape, same validation, same source of truth.
    """
    if explicit:
        return explicit
    # Lazy import: importing settings eagerly would pull in pydantic
    # validation for any other AUTH_* env vars even when the user
    # passes ``--api-key`` and doesn't need them.
    from qfa.settings import AuthSettings

    auth = AuthSettings()
    if not auth.api_keys:
        raise SystemExit(
            "No API key available: pass --api-key or set AUTH_API_KEYS "
            "to the same JSON the server consumes."
        )
    return auth.api_keys[0].key.get_secret_value()


# --- CLI ----------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"Path to corpus YAML (default: {DEFAULT_CORPUS.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="Number of records to sample"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Sampling seed (deterministic subset)"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Max in-flight requests (default 1 — safe first run)",
    )
    parser.add_argument(
        "--total-calls",
        type=int,
        default=None,
        help="Total requests to fire (default: same as --concurrency)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://localhost:8000",
        help="Server base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Override Bearer token (default: first AUTH_API_KEYS entry)",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=DEFAULT_PROMPT,
        help="Analyst prompt sent on every request",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=("hierarchical", "single_pass"),
        default="hierarchical",
        help="Analysis mode (default: hierarchical)",
    )
    parser.add_argument(
        "--period",
        type=str,
        choices=("day", "week", "month"),
        default=None,
        help="Coding-trend granularity (default: server-side default)",
    )
    parser.add_argument(
        "--no-anonymize",
        action="store_true",
        help="Disable PII anonymisation (default: enabled)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-request httpx timeout in seconds (default {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory for raw .jsonl results "
            f"(default {DEFAULT_OUTPUT_DIR.relative_to(REPO_ROOT)})"
        ),
    )
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    api_key = resolve_api_key(args.api_key)
    records = load_sample(args.corpus, args.limit, args.seed)
    logger.info(
        "Sampled %d records from %s (seed=%d). Firing %d requests "
        "at %s with concurrency=%d.",
        len(records),
        args.corpus,
        args.seed,
        args.total_calls or args.concurrency,
        args.base_url,
        args.concurrency,
    )

    results = await run_batch(
        records,
        base_url=args.base_url,
        api_key=api_key,
        concurrency=args.concurrency,
        timeout_s=args.timeout,
        prompt=args.prompt,
        mode=args.mode,
        anonymize=not args.no_anonymize,
        period=args.period,
        total_calls=args.total_calls,
    )

    out_path = write_jsonl(results, args.output_dir)
    summary = summarize(results)
    print(format_summary(summary))
    print(f"Raw results: {out_path.relative_to(REPO_ROOT)}")
    return 0 if summary.failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, fire requests, print summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
