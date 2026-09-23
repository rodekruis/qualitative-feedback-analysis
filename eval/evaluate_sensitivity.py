"""Baseline evaluation for the sensitivity endpoint."""

import argparse
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from langfuse import Evaluation, get_client

from _common import MAX_CONCURRENCY, load_env, resolve_config, run_metadata

EXPERIMENT_NAME = "sensitivity-baseline"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the QFA sensitivity endpoint against a Langfuse dataset.",
        epilog=(
            "Examples:\n"
            "  uv run python eval/evaluate_sensitivity.py "
            "--dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords --smoke-limit 5\n"
            "\n"
            "  uv run python eval/evaluate_sensitivity.py "
            "--dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--dataset",
        required=True,
        help="Name of the Langfuse dataset to evaluate.",
    )
    parser.add_argument(
        "--smoke-limit",
        type=int,
        default=None,
        help="Run only the first N records for local smoke testing. Default: full dataset.",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="Optional Langfuse run name.",
    )

    return parser.parse_args()


def _label_to_bool(label: object) -> bool:
    """Convert the human Langfuse label to a boolean."""
    if not isinstance(label, str):
        raise ValueError(f"Expected string label, got {label!r}")

    normalized = label.strip().lower()

    if normalized == "sensitive":
        return True
    if normalized == "not sensitive":
        return False

    raise ValueError(f"Unknown expected label: {label!r}")


def _make_detect_sensitive(
    base_url: str, api_key: str
) -> Callable[..., dict[str, Any]]:
    """Bind ``base_url``/``api_key`` into a task matching Langfuse's ``TaskFunction`` shape."""

    def detect_sensitive(*, item: Any, **kwargs: Any) -> dict[str, Any]:
        """Call the current production-style sensitivity endpoint."""
        if not isinstance(item.input, str):
            raise ValueError(
                f"Expected dataset input to be a string, got {type(item.input)}"
            )

        response = httpx.post(
            f"{base_url}/v1/detect-sensitive",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "feedback_record": {
                    "id": str(item.id),
                    "content": item.input,
                }
            },
            timeout=300,
        )
        response.raise_for_status()

        result = response.json()

        return {
            "label": "Sensitive" if result["is_sensitive"] else "Not sensitive",
            "is_sensitive": result["is_sensitive"],
            "sensitivity_types": result["sensitivity_types"],
            "explanation": result["explanation"],
        }

    return detect_sensitive


def correctness_evaluator(
    *,
    output: dict[str, Any],
    expected_output: Any,
    **kwargs: Any,
) -> Evaluation:
    """Score whether the binary prediction matches the human label."""
    expected = _label_to_bool(expected_output)
    predicted = bool(output["is_sensitive"])

    return Evaluation(
        name="correct",
        value=1.0 if predicted == expected else 0.0,
        comment=(
            f"Expected: {'Sensitive' if expected else 'Not sensitive'}; "
            f"predicted: {'Sensitive' if predicted else 'Not sensitive'}"
        ),
    )


def classification_case_evaluator(
    *,
    output: dict[str, Any],
    expected_output: Any,
    **kwargs: Any,
) -> Evaluation:
    """Record TP, TN, FP or FN for easy filtering in Langfuse."""
    expected = _label_to_bool(expected_output)
    predicted = bool(output["is_sensitive"])

    if expected and predicted:
        case = "TP"
    elif not expected and not predicted:
        case = "TN"
    elif not expected and predicted:
        case = "FP"
    else:
        case = "FN"

    return Evaluation(
        name="classification_case",
        value=case,
        data_type="CATEGORICAL",
    )


def _get_predictions(item_results: Any) -> list[tuple[bool, bool, dict[str, Any]]]:
    """Extract successful expected/predicted pairs."""
    predictions: list[tuple[bool, bool, dict[str, Any]]] = []

    for result in item_results:
        output = result.output

        if not isinstance(output, dict) or "is_sensitive" not in output:
            continue

        expected = _label_to_bool(result.item.expected_output)
        predicted = bool(output["is_sensitive"])

        predictions.append((expected, predicted, output))

    return predictions


def _divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def binary_metrics_evaluator(*, item_results: Any, **kwargs: Any) -> list[Evaluation]:
    """Calculate aggregate binary classification metrics."""
    predictions = _get_predictions(item_results)

    tp = sum(expected and predicted for expected, predicted, _ in predictions)
    tn = sum(not expected and not predicted for expected, predicted, _ in predictions)
    fp = sum(not expected and predicted for expected, predicted, _ in predictions)
    fn = sum(expected and not predicted for expected, predicted, _ in predictions)

    precision = _divide(tp, tp + fp)
    recall = _divide(tp, tp + fn)
    recall_not_sensitive = _divide(tn, tn + fp)
    accuracy = _divide(tp + tn, len(predictions))
    f1 = _divide(2 * precision * recall, precision + recall)

    counts = {
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
        "n": len(predictions),
    }

    return [
        Evaluation(
            name="accuracy",
            value=accuracy,
            metadata=counts,
        ),
        Evaluation(
            name="precision_sensitive",
            value=precision,
            metadata=counts,
        ),
        Evaluation(
            name="recall_sensitive",
            value=recall,
            metadata=counts,
        ),
        Evaluation(
            name="recall_not_sensitive",
            value=recall_not_sensitive,
            metadata=counts,
        ),
        Evaluation(
            name="f1_sensitive",
            value=f1,
            metadata=counts,
        ),
    ]


def sensitivity_type_distribution_evaluator(
    *, item_results: Any, **kwargs: Any
) -> list[Evaluation]:
    """Describe which sensitivity types drive sensitive predictions."""
    predictions = _get_predictions(item_results)

    all_types: Counter[str] = Counter()
    true_positive_types: Counter[str] = Counter()
    false_positive_types: Counter[str] = Counter()

    predicted_sensitive = 0

    for expected, predicted, output in predictions:
        if not predicted:
            continue

        predicted_sensitive += 1

        # Count each type at most once per feedback record.
        types = set(output.get("sensitivity_types", []))

        all_types.update(types)

        if expected:
            true_positive_types.update(types)
        else:
            false_positive_types.update(types)

    rates = (
        {
            sensitivity_type: count / predicted_sensitive
            for sensitivity_type, count in all_types.items()
        }
        if predicted_sensitive
        else {}
    )

    mean_types = (
        sum(all_types.values()) / predicted_sensitive if predicted_sensitive else 0.0
    )

    return [
        Evaluation(
            name="n_distinct_sensitivity_types",
            value=len(all_types),
            comment="Distribution stored in score metadata.",
            metadata={
                "counts": dict(all_types.most_common()),
                "share_of_sensitive_predictions": dict(
                    sorted(
                        rates.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                ),
                "true_positive_counts": dict(true_positive_types.most_common()),
                "false_positive_counts": dict(false_positive_types.most_common()),
            },
        ),
        Evaluation(
            name="mean_sensitivity_types_per_flagged_record",
            value=mean_types,
        ),
    ]


def main() -> None:
    """Run the experiment against a Langfuse dataset."""
    args = _parse_args()

    load_env()
    base_url, api_key = resolve_config()

    langfuse = get_client()
    dataset = langfuse.get_dataset(args.dataset)

    if args.smoke_limit is not None:
        if args.smoke_limit <= 0:
            raise SystemExit("--smoke-limit must be greater than 0.")
        items = dataset.items[: args.smoke_limit]
    else:
        items = dataset.items

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    if args.run_name:
        run_name = args.run_name
    elif args.smoke_limit:
        run_name = f"smoke-{args.smoke_limit}-{timestamp}"
    else:
        run_name = f"baseline-full-{timestamp}"

    print(f"Dataset: {args.dataset}")
    print(f"Backend: {base_url}")
    print(f"Records: {len(items)}")
    print(f"Run:     {run_name}")
    print()

    result = langfuse.run_experiment(
        name=EXPERIMENT_NAME,
        run_name=run_name,
        data=items,
        task=_make_detect_sensitive(base_url, api_key),
        evaluators=[
            correctness_evaluator,
            classification_case_evaluator,
        ],
        run_evaluators=[
            binary_metrics_evaluator,
            sensitivity_type_distribution_evaluator,
        ],
        max_concurrency=MAX_CONCURRENCY,
        metadata=run_metadata(
            base_url,
            dataset=args.dataset,
            endpoint="/v1/detect-sensitive",
            evaluation="baseline",
            smoke_limit=args.smoke_limit,
        ),
    )

    print(result.format())

    if result.dataset_run_url:
        print(f"\nLangfuse: {result.dataset_run_url}")


if __name__ == "__main__":
    main()
