"""Additional sensitivity-type analysis for the sensitivity evaluation."""

from collections import Counter
from typing import Any

from langfuse import Evaluation


def _label_to_bool(label: object) -> bool:
    """Convert a human sensitivity label to a boolean."""
    if not isinstance(label, str):
        raise ValueError(f"Expected string label, got {label!r}")

    normalized = label.strip().lower()

    if normalized == "sensitive":
        return True
    if normalized == "not sensitive":
        return False

    raise ValueError(f"Unknown expected label: {label!r}")


def sensitivity_type_item_evaluator(
    *,
    output: dict[str, Any],
    expected_output: Any,
    **kwargs: Any,
) -> list[Evaluation]:
    """Store sensitivity types as Langfuse scores for plotting.

    Only predicted-sensitive records have sensitivity types.

    For every predicted sensitivity type, this creates:
    - sensitivity_type: all predicted sensitivity types
    - sensitivity_type_tp: types occurring on true positives
    - sensitivity_type_fp: types occurring on false positives
    """
    predicted = bool(output["is_sensitive"])

    if not predicted:
        return []

    expected = _label_to_bool(expected_output)

    # Count a sensitivity type at most once per feedback record.
    sensitivity_types = sorted(set(output.get("sensitivity_types", [])))

    evaluations: list[Evaluation] = []

    for sensitivity_type in sensitivity_types:
        # Overall predicted sensitivity-type distribution.
        evaluations.append(
            Evaluation(
                name="sensitivity_type",
                value=sensitivity_type,
                data_type="CATEGORICAL",
            )
        )

        # Split the type distribution into true and false positives.
        evaluations.append(
            Evaluation(
                name="sensitivity_type_tp" if expected else "sensitivity_type_fp",
                value=sensitivity_type,
                data_type="CATEGORICAL",
            )
        )

    return evaluations


def sensitivity_type_fp_share_evaluator(
    *,
    item_results: Any,
    **kwargs: Any,
) -> list[Evaluation]:
    """Calculate the false-positive share for every sensitivity type.

    FP share = FP / (TP + FP)

    The denominator is the number of predicted-sensitive records for which
    the model returned that sensitivity type.
    """
    tp_counts: Counter[str] = Counter()
    fp_counts: Counter[str] = Counter()

    # Reuse the item-level scores created above instead of re-deriving
    # the classification from the raw outputs.
    for result in item_results:
        for evaluation in result.evaluations:
            if evaluation.name == "sensitivity_type_tp":
                tp_counts[str(evaluation.value)] += 1

            elif evaluation.name == "sensitivity_type_fp":
                fp_counts[str(evaluation.value)] += 1

    all_types = set(tp_counts) | set(fp_counts)

    evaluations: list[Evaluation] = []

    for sensitivity_type in sorted(all_types):
        tp = tp_counts[sensitivity_type]
        fp = fp_counts[sensitivity_type]
        total = tp + fp

        fp_share = fp / total if total else 0.0

        evaluations.append(
            Evaluation(
                name=f"sensitivity_type_fp_share::{sensitivity_type}",
                value=fp_share,
                metadata={
                    "sensitivity_type": sensitivity_type,
                    "true_positives": tp,
                    "false_positives": fp,
                    "total_predictions_with_type": total,
                },
            )
        )

    return evaluations
