"""Plant designed temporal trends into ``fixtures/analyze_corpus.yaml``.

Helper script (run manually, not in CI). It turns the corpus into a labelled
benchmark for trend detection on ``POST /v1/analyze`` with
``mode=hierarchical``.

For each record the script adds an ISO ``creation_date`` (string,
``YYYY-MM-DD``) to ``metadata`` and sets ``year`` consistent with that date.
Where the record carries one of the auto-selected "carrier" codes, the date is
sampled from a per-pattern monthly density so the resulting time series shows
a known shape. Records on codes not chosen for any pattern get uniformly
random dates and form the baseline noise.

No record is re-tagged: every record keeps its original ``codes`` field. The
patterns are constructed only by *which dates* we give to *which existing
records*. Cross-code patterns work because the script picks sibling leaf
codes that already share a parent in the colon-hierarchical coding framework.

The script writes the modified YAML in place, prepends a YAML comment block
documenting which leaf code carries which pattern, and saves a matplotlib PNG
of the planted trends next to the YAML.

Run::

    uv run python scripts/plant_trends_in_corpus.py [--seed N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import yaml

logger = logging.getLogger("plant_trends")

# --- Timeline -----------------------------------------------------------------

# A 12-month window. The fixture only has ~1000 records and the busiest leaf
# code carries 29 of them, so 24 monthly buckets would halve the per-bucket
# density and bury the planted signals in noise. One year (Jan-Dec 2020)
# gives ~2.4 records/month on a 29-record code — enough for a 6x rise or 8x
# spike to be visible while still respecting the user's authorisation to
# concentrate ``year`` values for density.
START = dt.date(2020, 1, 1)
END = dt.date(2020, 12, 31)
N_MONTHS = (END.year - START.year) * 12 + (END.month - START.month) + 1


# --- Pattern parameters -------------------------------------------------------
# All density functions take a month-index array ``t`` in [0, N_MONTHS) and
# return un-normalised weights. They include a positive baseline so the time
# series is "dirty" (no perfectly empty months in the pattern).

# Spike: Gaussian bump on top of a low baseline.
SPIKE_CENTER_MONTH = 5  # mid-year (June 2020)
SPIKE_WIDTH_MONTHS = 1.0
SPIKE_PEAK_OVER_BASE = 8.0

# Emerging: linearly rising density.
EMERGING_END_OVER_START = 6.0

# Declining: linearly falling density.
DECLINING_END_OVER_START = 1 / 6.0

# Step: low rate before STEP_MONTH, high rate after.
STEP_MONTH = 7  # August 2020 (month index 7 of 12)
STEP_HIGH_OVER_LOW = 5.0

# Rumour outliers: tight cluster window in days, placed mid-timeline.
RUMOUR_CLUSTER_DAYS = 2
RUMOUR_OFFSET_FROM_EDGE_DAYS = 60

# Cross-code: how many sibling leaf codes to spread the pattern across.
CROSS_CODE_N_SIBLINGS = 3


# --- Code-selection thresholds ------------------------------------------------

# Calibrated to the actual count distribution of fixtures/analyze_corpus.yaml
# (top leaf carries 29 records, 10 leaves carry >=15, 12 parents have >=3
# siblings >=5). These thresholds let all six patterns coexist without
# starving each other; tighten them if a richer corpus becomes available.
MIN_CARRIER_RECORDS = 15  # emerging / declining / step
MIN_SPIKE_RECORDS = 15
MIN_CROSS_CODE_PER_SIBLING = 5
RUMOUR_RECORDS_RANGE = (3, 12)


# --- Density functions --------------------------------------------------------


def _spike_density(t: np.ndarray) -> np.ndarray:
    """Gaussian bump on a low flat baseline; dirty by construction."""
    bump = SPIKE_PEAK_OVER_BASE * np.exp(
        -0.5 * ((t - SPIKE_CENTER_MONTH) / SPIKE_WIDTH_MONTHS) ** 2
    )
    return 1.0 + bump


def _emerging_density(t: np.ndarray) -> np.ndarray:
    """Linear rise from baseline 1.0 to ``EMERGING_END_OVER_START``."""
    return np.linspace(1.0, EMERGING_END_OVER_START, t.size)


def _declining_density(t: np.ndarray) -> np.ndarray:
    """Linear fall from 1.0 to ``DECLINING_END_OVER_START`` (i.e., still > 0)."""
    return np.linspace(1.0, DECLINING_END_OVER_START, t.size)


def _step_density(t: np.ndarray) -> np.ndarray:
    """Low rate before ``STEP_MONTH``, high rate after."""
    return np.where(t < STEP_MONTH, 1.0, STEP_HIGH_OVER_LOW).astype(float)


def _baseline_density(t: np.ndarray) -> np.ndarray:
    """Uniform over the timeline."""
    return np.ones_like(t, dtype=float)


# Pattern → density function. ``cross_code`` re-uses the emerging shape; the
# difference is that records on *several* sibling codes share that density, so
# any single code's series shows only a fraction of the rise.
PATTERN_DENSITIES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "spike": _spike_density,
    "emerging": _emerging_density,
    "declining": _declining_density,
    "step": _step_density,
    "cross_code": _emerging_density,
    "baseline": _baseline_density,
}

# Priority for resolving records carrying multiple codes assigned to different
# patterns: smaller / harder-to-place patterns win so their signal isn't
# diluted.
PATTERN_PRIORITY: list[str] = [
    "rumour",
    "cross_code",
    "spike",
    "step",
    "emerging",
    "declining",
    "baseline",
]


# --- Helpers ------------------------------------------------------------------


def _record_leaves(record: dict[str, Any]) -> list[str]:
    """Return the list of leaf code strings carried by a record."""
    raw = record.get("metadata", {}).get("codes", "") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def _parent_path(leaf: str) -> str:
    """Drop the last colon segment to get the parent in the code hierarchy."""
    return ":".join(leaf.split(":")[:-1])


def _leaf_short(leaf: str) -> str:
    """The last colon segment — readable label for plots / tables."""
    return leaf.split(":")[-1]


def _siblings_by_parent(counts: Counter[str]) -> dict[str, list[str]]:
    """Group leaf codes by their parent path."""
    sib: dict[str, list[str]] = defaultdict(list)
    for leaf in counts:
        sib[_parent_path(leaf)].append(leaf)
    return sib


# --- Code selection -----------------------------------------------------------


def select_carrier_codes(
    counts: Counter[str], rng: random.Random
) -> dict[str, list[str]]:
    """Pick a carrier leaf code (or sibling set) for each pattern.

    Returns a mapping ``pattern -> [leaf, ...]``. Single-code patterns get a
    one-element list; ``cross_code`` gets ``CROSS_CODE_N_SIBLINGS`` siblings;
    ``baseline`` is implicit (everything not assigned to a pattern).

    The picks are deterministic given the ``rng`` seed: codes are first
    filtered by the per-pattern minimum count and then chosen by a stable
    sort + random pick from the eligible set, so a different seed picks
    a different code from the eligible set rather than reshuffling order.
    """
    used: set[str] = set()

    def pick(eligible: list[str]) -> str:
        eligible_sorted = sorted(c for c in eligible if c not in used)
        if not eligible_sorted:
            raise RuntimeError(
                "No eligible code left for this pattern; data may be insufficient."
            )
        chosen = rng.choice(eligible_sorted)
        used.add(chosen)
        return chosen

    # Cross-code: a parent with at least N siblings each meeting the min count.
    sib = _siblings_by_parent(counts)
    cross_candidates = {
        parent: sorted(
            (leaf for leaf in leaves if counts[leaf] >= MIN_CROSS_CODE_PER_SIBLING),
            key=lambda c: -counts[c],
        )
        for parent, leaves in sib.items()
    }
    cross_candidates = {
        parent: leaves[:CROSS_CODE_N_SIBLINGS]
        for parent, leaves in cross_candidates.items()
        if len(leaves) >= CROSS_CODE_N_SIBLINGS
    }
    if not cross_candidates:
        raise RuntimeError(
            "No parent has enough sibling leaf codes for a cross-code pattern."
        )
    cross_parent = rng.choice(sorted(cross_candidates))
    cross_siblings = cross_candidates[cross_parent]
    used.update(cross_siblings)

    # Single-code patterns.
    spike = pick([leaf for leaf, n in counts.items() if n >= MIN_SPIKE_RECORDS])
    emerging = pick([leaf for leaf, n in counts.items() if n >= MIN_CARRIER_RECORDS])
    declining = pick([leaf for leaf, n in counts.items() if n >= MIN_CARRIER_RECORDS])
    step = pick([leaf for leaf, n in counts.items() if n >= MIN_CARRIER_RECORDS])

    # Rumour: a low-volume leaf so the cluster stands out.
    rumour_candidates = [
        leaf
        for leaf, n in counts.items()
        if RUMOUR_RECORDS_RANGE[0] <= n <= RUMOUR_RECORDS_RANGE[1] and leaf not in used
    ]
    if not rumour_candidates:
        raise RuntimeError(
            f"No leaf code with {RUMOUR_RECORDS_RANGE[0]}-{RUMOUR_RECORDS_RANGE[1]} "
            "records left for the rumour pattern."
        )
    rumour = rng.choice(sorted(rumour_candidates))
    used.add(rumour)

    return {
        "spike": [spike],
        "emerging": [emerging],
        "declining": [declining],
        "step": [step],
        "cross_code": cross_siblings,
        "rumour": [rumour],
    }


# --- Assignment + sampling ----------------------------------------------------


def _months_to_date(month_idx: int, rng: random.Random) -> dt.date:
    """Pick a uniform-random day within the given month bucket."""
    year = START.year + (START.month - 1 + month_idx) // 12
    month = (START.month - 1 + month_idx) % 12 + 1
    next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    days_in_month = (next_month - dt.date(year, month, 1)).days
    return dt.date(year, month, rng.randint(1, days_in_month))


def _sample_dates_from_density(
    n: int,
    density_fn: Callable[[np.ndarray], np.ndarray],
    rng: random.Random,
) -> list[dt.date]:
    """Sample ``n`` calendar dates by month-bucket weights then uniform-in-month."""
    t = np.arange(N_MONTHS, dtype=float)
    weights = density_fn(t)
    weights = weights / weights.sum()
    months = rng.choices(range(N_MONTHS), weights=weights.tolist(), k=n)
    return [_months_to_date(m, rng) for m in months]


def _sample_rumour_dates(n: int, rng: random.Random) -> list[dt.date]:
    """Tight ``RUMOUR_CLUSTER_DAYS``-wide window placed mid-timeline."""
    span = (END - START).days
    start_offset = rng.randint(
        RUMOUR_OFFSET_FROM_EDGE_DAYS, span - RUMOUR_OFFSET_FROM_EDGE_DAYS
    )
    cluster_start = START + dt.timedelta(days=start_offset)
    return [
        cluster_start + dt.timedelta(days=rng.randint(0, RUMOUR_CLUSTER_DAYS - 1))
        for _ in range(n)
    ]


def assign_dates(
    records: list[dict[str, Any]],
    carriers: dict[str, list[str]],
    rng: random.Random,
) -> dict[str, list[int]]:
    """Mutate each record in place: add ``creation_date`` + update ``year``.

    Returns ``pattern -> [record_index, ...]`` so the caller can plot and
    summarise.
    """
    pattern_for_leaf: dict[str, str] = {}
    for pattern, leaves in carriers.items():
        for leaf in leaves:
            pattern_for_leaf[leaf] = pattern

    # Resolve each record's pattern by priority.
    rank = {p: i for i, p in enumerate(PATTERN_PRIORITY)}
    by_pattern: dict[str, list[int]] = defaultdict(list)
    for idx, record in enumerate(records):
        leaves = _record_leaves(record)
        candidates = {
            pattern_for_leaf[leaf] for leaf in leaves if leaf in pattern_for_leaf
        }
        pattern = min(candidates, key=lambda p: rank[p]) if candidates else "baseline"
        by_pattern[pattern].append(idx)

    # Sample dates per pattern.
    for pattern, indices in by_pattern.items():
        if pattern == "rumour":
            dates = _sample_rumour_dates(len(indices), rng)
        else:
            dates = _sample_dates_from_density(
                len(indices), PATTERN_DENSITIES[pattern], rng
            )
        for idx, date in zip(indices, dates, strict=True):
            iso = date.isoformat()
            records[idx]["metadata"]["creation_date"] = iso
            records[idx]["metadata"]["year"] = date.year

    return dict(by_pattern)


# --- Output: YAML + PNG -------------------------------------------------------


def build_top_comment(
    carriers: dict[str, list[str]],
    by_pattern: dict[str, list[int]],
) -> str:
    """A YAML comment block documenting the planted benchmark."""
    lines = [
        "# ============================================================================",
        "# This file contains 1000 synthetic feedback records.",
        "# Codes come from coding_framework.json (comma-separated, colon-hierarchical).",
        "#",
        "# PLANTED TRENDS (added by scripts/plant_trends_in_corpus.py).",
        "# Each record carries an ISO `creation_date` in metadata; `year` matches.",
        f"# Timeline: {START.isoformat()} .. {END.isoformat()} ({N_MONTHS} monthly buckets).",
        "#",
        "# | Pattern     | n  | Carrier leaf code(s)                                                | Expected detector behavior                                                                  |",
        "# |-------------|----|---------------------------------------------------------------------|---------------------------------------------------------------------------------------------|",
    ]
    expected: dict[str, str] = {
        "spike": f"per-code monthly bucket count should spike around month {SPIKE_CENTER_MONTH + 1} (peak ~{SPIKE_PEAK_OVER_BASE:.0f}x baseline)",
        "emerging": f"per-code monthly count rises ~{EMERGING_END_OVER_START:.0f}x from start to end",
        "declining": f"per-code monthly count falls to ~{DECLINING_END_OVER_START:.2f}x of start",
        "step": f"per-code monthly count jumps ~{STEP_HIGH_OVER_LOW:.0f}x at month index {STEP_MONTH + 1}",
        "cross_code": "no single sibling shows a strong rise; only aggregating across siblings reveals it (hardest test)",
        "rumour": f"a tight {RUMOUR_CLUSTER_DAYS}-day cluster of the same low-volume code, isolated in time",
    }
    for pattern in PATTERN_PRIORITY:
        if pattern == "baseline" or pattern not in carriers:
            continue
        n = len(by_pattern.get(pattern, []))
        leaves = " + ".join(_leaf_short(leaf) for leaf in carriers[pattern])
        lines.append(
            f"# | {pattern:<11} | {n:<2} | {leaves:<67} | {expected[pattern]:<91} |"
        )
    baseline_n = len(by_pattern.get("baseline", []))
    lines.append(
        f"# | baseline    | {baseline_n:<2} | (all other codes, uniformly random dates)                          | flat-ish; the noise floor                                                                |"
    )
    lines.append(
        "# ============================================================================"
    )
    return "\n".join(lines) + "\n"


def write_yaml(path: Path, records: list[dict[str, Any]], top_comment: str) -> None:
    """Dump records as block-style YAML, with the comment block prepended."""
    body = yaml.safe_dump(
        records,
        sort_keys=False,
        allow_unicode=True,
        width=110,
        default_flow_style=False,
    )
    path.write_text(top_comment + body, encoding="utf-8")


def make_plot(
    by_pattern: dict[str, list[int]],
    records: list[dict[str, Any]],
    carriers: dict[str, list[str]],
    out_png: Path,
) -> None:
    """Stacked subplots: monthly record count per planted pattern + baseline."""
    panels = [p for p in PATTERN_PRIORITY if p in by_pattern]
    n = len(panels)
    fig, axes = plt.subplots(n, 1, figsize=(11, 1.7 * n), sharex=True)
    if n == 1:
        axes = [axes]

    edges = [
        dt.date(
            START.year + (START.month - 1 + m) // 12, (START.month - 1 + m) % 12 + 1, 1
        )
        for m in range(N_MONTHS + 1)
    ]
    bin_edges = np.array([(d - START).days for d in edges])
    month_widths = np.diff(bin_edges)
    month_centers = bin_edges[:-1] + month_widths / 2

    TOTAL_COLOR = "#3a6ea5"
    SIBLING_COLORS = ["tab:orange", "tab:green", "tab:red"]

    for ax, pattern in zip(axes, panels, strict=True):
        indices = by_pattern[pattern]
        carrier_leaves = carriers.get(pattern, [])

        if pattern == "cross_code" and len(carrier_leaves) > 1:
            # Grouped bar chart: one bar per sibling code + a 'total' bar per
            # month. Per-sibling bars count records carrying that leaf (a
            # record carrying multiple cross-code siblings counts in each,
            # matching the coding-trend-table semantics). The 'total' bar
            # counts unique records assigned to the pattern — same number the
            # other panels would show with a single bar per month.
            n_groups = len(carrier_leaves) + 1
            slot = month_widths / n_groups
            bar_w = slot * 0.92
            offsets = [(k - (n_groups - 1) / 2.0) * slot for k in range(n_groups)]

            for i, leaf in enumerate(carrier_leaves):
                counts = np.zeros(N_MONTHS, dtype=int)
                for idx in indices:
                    if leaf not in _record_leaves(records[idx]):
                        continue
                    d = dt.date.fromisoformat(records[idx]["metadata"]["creation_date"])
                    m = (d.year - START.year) * 12 + (d.month - START.month)
                    counts[m] += 1
                ax.bar(
                    month_centers + offsets[i],
                    counts,
                    width=bar_w,
                    color=SIBLING_COLORS[i % len(SIBLING_COLORS)],
                    edgecolor="white",
                    linewidth=0.5,
                    label=_leaf_short(leaf),
                )

            totals = np.zeros(N_MONTHS, dtype=int)
            for idx in indices:
                d = dt.date.fromisoformat(records[idx]["metadata"]["creation_date"])
                m = (d.year - START.year) * 12 + (d.month - START.month)
                totals[m] += 1
            ax.bar(
                month_centers + offsets[-1],
                totals,
                width=bar_w,
                color=TOTAL_COLOR,
                edgecolor="white",
                linewidth=0.5,
                label="total",
            )
            ax.legend(loc="upper left", fontsize=7, ncols=2, frameon=False)
        else:
            dates = [
                dt.date.fromisoformat(records[i]["metadata"]["creation_date"])
                for i in indices
            ]
            days = (
                np.array([(d - START).days for d in dates]) if dates else np.array([])
            )
            ax.hist(days, bins=bin_edges, color=TOTAL_COLOR, edgecolor="white")

        carrier_label = (
            "all other codes"
            if pattern == "baseline"
            else " + ".join(_leaf_short(c) for c in carriers[pattern])
        )
        ax.set_title(
            f"{pattern}  (n={len(indices)})  —  {carrier_label}",
            fontsize=10,
            loc="left",
        )
        ax.set_ylabel("records / month")
        ax.tick_params(labelsize=8)

    # X-axis: show year-month labels every 3 months.
    label_idx = list(range(0, N_MONTHS, 3))
    label_days = [bin_edges[i] for i in label_idx]
    label_text = [f"{edges[i].year}-{edges[i].month:02d}" for i in label_idx]
    axes[-1].set_xticks(label_days)
    axes[-1].set_xticklabels(label_text, rotation=30, ha="right")
    axes[-1].set_xlabel("month")

    fig.suptitle("Planted trends in analyze_corpus.yaml", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120, bbox_inches="tight")
    plt.close(fig)


# --- Main ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--yaml",
        type=Path,
        default=Path("fixtures/analyze_corpus.yaml"),
        help="Path to the corpus YAML to modify in place.",
    )
    parser.add_argument(
        "--png",
        type=Path,
        default=None,
        help="Where to write the trend plot (default: <yaml>.trends.png).",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    yaml_path: Path = args.yaml
    png_path: Path = args.png or yaml_path.with_suffix(".trends.png")

    # PRNG is used purely to generate reproducible synthetic benchmark data;
    # cryptographic strength is not required (and would defeat reproducibility).
    rng = random.Random(args.seed)  # noqa: S311

    logger.info("Loading %s", yaml_path)
    records: list[dict[str, Any]] = yaml.safe_load(yaml_path.read_text())
    logger.info("Loaded %d records", len(records))

    counts = Counter()
    for r in records:
        counts.update(_record_leaves(r))
    logger.info("Distinct leaf codes: %d", len(counts))

    carriers = select_carrier_codes(counts, rng)
    logger.info("Chosen carriers:")
    for pattern, leaves in carriers.items():
        for leaf in leaves:
            logger.info("  %-11s %4d  %s", pattern, counts[leaf], leaf)

    by_pattern = assign_dates(records, carriers, rng)
    top_comment = build_top_comment(carriers, by_pattern)

    logger.info("Per-pattern record counts:")
    for pattern in PATTERN_PRIORITY:
        if pattern in by_pattern:
            logger.info("  %-11s %4d", pattern, len(by_pattern[pattern]))

    logger.info("Writing %s", yaml_path)
    write_yaml(yaml_path, records, top_comment)

    logger.info("Plotting %s", png_path)
    make_plot(by_pattern, records, carriers, png_path)

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
