r"""Generate the COVID-19 trend-detection benchmark corpus.

Helper script (run manually, not in CI). The output is a labelled benchmark
for ``POST /v1/analyze`` with ``mode=hierarchical``: every record carries a
known ``created`` and a fraction of records sit on auto-selected
"carrier" codes whose dates trace a designed temporal pattern (spike,
emerging, declining, step, cross-code, rumour).

The script exposes three subcommands; together they form a deterministic
two-stage pipeline that splits *trend statistics* (Python, here) from
*creative prose* (an LLM, via ``scripts/generate_corpus.prompt.md``):

``gen-specs``
    Allocate leaf-code volumes against ``fixtures/coding_framework.json``,
    sample metadata (region, country, source, sensitive, …) per record,
    pick carriers, sample ``created`` from each pattern's monthly
    density, and emit one JSON object per record to a JSONL file. The
    specs carry everything except ``text`` and ``sentence_count``.

``merge``
    Read the specs JSONL + an LLM-produced ``texts.jsonl`` (one
    ``{id, text, sentence_count}`` per line). Join by ``id``, validate
    coverage, attach the texts, and write the final YAML + trend-plot PNG.

``plant-in-place``
    Original behaviour: load an existing YAML, add ``created`` to
    every record by sampling from per-pattern densities, write the YAML
    back in place. Useful for retro-fitting a real-world fixture as a
    labelled benchmark without regenerating prose. For each record the
    script adds an ISO ``created`` (string, ``YYYY-MM-DD``) to
    ``metadata`` and sets ``year`` consistent with that date. No record
    is re-tagged: every record keeps its original ``codes`` field.

Run::

    uv run python scripts/generate_corpus.py gen-specs \\
        --output fixtures/analyze_corpus.specs.jsonl
    uv run python scripts/generate_corpus.py merge \\
        --specs fixtures/analyze_corpus.specs.jsonl \\
        --texts texts.jsonl \\
        --output fixtures/analyze_corpus.yaml
    uv run python scripts/generate_corpus.py plant-in-place [--seed N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import yaml

logger = logging.getLogger("generate_corpus")

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


@dataclass(frozen=True)
class CarrierThresholds:
    """Minimum record counts a leaf must carry to be eligible per pattern.

    Different corpora warrant different thresholds. The defaults below match
    ``fixtures/analyze_corpus.yaml`` (1000 records, top leaf carries 29,
    10 leaves carry >=15, 12 parents have >=3 siblings >=5) so that
    ``plant-in-place`` keeps working unchanged. ``gen-specs`` overrides
    them upward to match the denser corpus it produces.
    """

    min_carrier: int = 15  # emerging / declining / step
    min_spike: int = 15
    min_cross_code_per_sibling: int = 5
    cross_code_n_siblings: int = 3
    rumour_range: tuple[int, int] = (3, 12)


# Module-level defaults preserved as constants so the existing top-comment
# builder (which prints e.g. "tight 2-day cluster") can read the rumour range
# without threading a thresholds object through every helper.
DEFAULT_THRESHOLDS = CarrierThresholds()
MIN_CARRIER_RECORDS = DEFAULT_THRESHOLDS.min_carrier
MIN_SPIKE_RECORDS = DEFAULT_THRESHOLDS.min_spike
MIN_CROSS_CODE_PER_SIBLING = DEFAULT_THRESHOLDS.min_cross_code_per_sibling
RUMOUR_RECORDS_RANGE = DEFAULT_THRESHOLDS.rumour_range


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
    counts: Counter[str],
    rng: random.Random,
    thresholds: CarrierThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, list[str]]:
    """Pick a carrier leaf code (or sibling set) for each pattern.

    Returns a mapping ``pattern -> [leaf, ...]``. Single-code patterns get a
    one-element list; ``cross_code`` gets ``thresholds.cross_code_n_siblings``
    siblings; ``baseline`` is implicit (everything not assigned to a pattern).

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
            (
                leaf
                for leaf in leaves
                if counts[leaf] >= thresholds.min_cross_code_per_sibling
            ),
            key=lambda c: -counts[c],
        )
        for parent, leaves in sib.items()
    }
    cross_candidates = {
        parent: leaves[: thresholds.cross_code_n_siblings]
        for parent, leaves in cross_candidates.items()
        if len(leaves) >= thresholds.cross_code_n_siblings
    }
    if not cross_candidates:
        raise RuntimeError(
            "No parent has enough sibling leaf codes for a cross-code pattern."
        )
    cross_parent = rng.choice(sorted(cross_candidates))
    cross_siblings = cross_candidates[cross_parent]
    used.update(cross_siblings)

    # Single-code patterns.
    spike = pick([leaf for leaf, n in counts.items() if n >= thresholds.min_spike])
    emerging = pick([leaf for leaf, n in counts.items() if n >= thresholds.min_carrier])
    declining = pick(
        [leaf for leaf, n in counts.items() if n >= thresholds.min_carrier]
    )
    step = pick([leaf for leaf, n in counts.items() if n >= thresholds.min_carrier])

    # Rumour: a low-volume leaf so the cluster stands out.
    lo, hi = thresholds.rumour_range
    rumour_candidates = [
        leaf for leaf, n in counts.items() if lo <= n <= hi and leaf not in used
    ]
    if not rumour_candidates:
        raise RuntimeError(
            f"No leaf code with {lo}-{hi} records left for the rumour pattern."
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
    """Mutate each record in place: add ``created`` + update ``year``.

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
            records[idx]["metadata"]["created"] = iso
            records[idx]["metadata"]["year"] = date.year

    return dict(by_pattern)


# --- Output: YAML + PNG -------------------------------------------------------


def build_top_comment(
    carriers: dict[str, list[str]],
    by_pattern: dict[str, list[int]],
    total: int | None = None,
) -> str:
    """A YAML comment block documenting the planted benchmark."""
    if total is None:
        total = sum(len(v) for v in by_pattern.values())
    lines = [
        "# ============================================================================",
        f"# This file contains {total} synthetic feedback records.",
        "# Codes come from coding_framework.json (comma-separated, colon-hierarchical).",
        "#",
        "# PLANTED TRENDS (added by scripts/generate_corpus.py).",
        "# Each record carries an ISO `created` in metadata; `year` matches.",
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
    title: str = "Planted trends in analyze_corpus.yaml",
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
                    d = dt.date.fromisoformat(records[idx]["metadata"]["created"])
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
                d = dt.date.fromisoformat(records[idx]["metadata"]["created"])
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
                dt.date.fromisoformat(records[i]["metadata"]["created"])
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

    fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120, bbox_inches="tight")
    plt.close(fig)


# --- gen-specs: deterministic spec generation --------------------------------

# Thresholds for ``gen-specs``: chosen so every pattern has comfortable
# headroom on a ~5000-record corpus with the SPEC_TIERS allocation below.
# Tighter than DEFAULT_THRESHOLDS — these would starve the existing
# 1000-record fixture, which is why each subcommand picks its own.
SPEC_THRESHOLDS = CarrierThresholds(
    min_carrier=40,
    min_spike=40,
    min_cross_code_per_sibling=40,
    cross_code_n_siblings=3,
    rumour_range=(4, 10),
)

# Tiered volume per leaf, in priority order. Leaves are deterministically
# shuffled, then handed out one tier at a time. The leftover tail gets the
# fallback floor in ``_allocate_volumes``.
SPEC_TIERS: list[tuple[int, int]] = [
    (5, 130),  # top tier: 5 leaves * 130 = 650
    (25, 60),  # mid-top:  25 leaves * 60 = 1500
    (70, 30),  # mid:      70 leaves * 30 = 2100
]
SPEC_TAIL_FLOOR = 20  # records per leaf for any "other" leaf past the tiers
SPEC_RUMOUR_LEAVES = 4
SPEC_CROSS_PER_SIBLING_TARGET = 45  # > min_cross_code_per_sibling, < tier mid
SPEC_TOTAL_DEFAULT = 5000

# Per-record metadata sampling distributions. English-only per the current
# corpus policy (the LLM prompt will write English regardless of this field,
# but keeping the spec consistent avoids confusing downstream consumers).
LANGUAGE_DIST: dict[str, float] = {"en": 1.0}
SOURCE_DIST: dict[str, float] = {
    "hotline": 0.80,
    "web form": 0.05,
    "email": 0.05,
    "outbound call": 0.05,
    "survey": 0.025,
    "focus group": 0.015,
    "community feedback session": 0.005,
    "volunteer report": 0.005,
}
COUNTRY_REGIONS: dict[str, list[str]] = {
    "Sierra Leone": [
        "Western Area",
        "Bo District",
        "Kenema District",
        "Kambia District",
    ],
    "DRC": ["Nord-Kivu", "Sud-Kivu", "Ituri", "Tshopo"],
    "Senegal": ["Dakar", "Thiès", "Saint-Louis", "Ziguinchor"],
    "Liberia": ["Montserrado", "Bong", "Nimba", "Grand Bassa"],
    "Burkina Faso": [
        "Centre",
        "Hauts-Bassins",
        "Boucle du Mouhoun",
        "Sahel",
    ],
    "Guinea": ["Conakry", "Kindia", "Nzérékoré", "Mamou"],
    "Côte d'Ivoire": ["Abidjan", "Bouaké", "Daloa", "Korhogo"],
}
SENSITIVE_FLIP_PROB = 0.05  # invert the leaf's default ``sensitive`` ~5% of records


@dataclass(frozen=True)
class LeafSpec:
    """One leaf code from coding_framework.json with the parents we need."""

    code_id: str
    feedback_type: str  # top-level type, e.g. "Observation, perception or belief"
    category: str
    name: str
    description: str
    examples: list[str] = field(default_factory=list)
    sensitive_default: bool = False


def _load_framework(path: Path) -> list[LeafSpec]:
    """Enumerate every leaf code in the framework with the metadata we need."""
    data = json.loads(path.read_text(encoding="utf-8"))
    leaves: list[LeafSpec] = []
    for ftype in data.get("types", []):
        type_name = ftype.get("name", "")
        for category in ftype.get("categories", []):
            cat_name = category.get("name", "")
            for code in category.get("codes", []):
                leaves.append(
                    LeafSpec(
                        code_id=code["code_id"],
                        feedback_type=type_name,
                        category=cat_name,
                        name=code.get("name", ""),
                        description=code.get("code_description", "") or "",
                        examples=list(code.get("examples", []) or []),
                        sensitive_default=bool(code.get("sensitive", False)),
                    )
                )
    return leaves


def _allocate_volumes(
    leaves: list[LeafSpec],
    total: int,
    thresholds: CarrierThresholds,
    rng: random.Random,
) -> dict[str, int]:
    """Assign per-leaf record counts so the gen-specs targets are met.

    The allocation is built up in four steps so the final counts simultaneously
    meet every pattern's eligibility window:

      1. Pick a cross-code parent that has at least
         ``thresholds.cross_code_n_siblings`` leaves, lock those siblings at
         ``SPEC_CROSS_PER_SIBLING_TARGET`` each.
      2. Reserve ``SPEC_RUMOUR_LEAVES`` low-volume leaves, each at a count
         inside ``thresholds.rumour_range``.
      3. Distribute the remaining leaves through ``SPEC_TIERS`` so the top
         tier has codes large enough for spike / emerging / declining / step.
      4. Pad any leftover leaves with ``SPEC_TAIL_FLOOR``, then nudge the
         largest "other" leaves up or down to hit ``total`` exactly.

    Without the explicit cross-code reservation in step 1 the LLM-driven
    text generation has no guarantee of producing a parent with three
    similarly-sized siblings — the random tier assignment could spread those
    siblings across three different tiers, breaking the cross-code test.
    """
    by_parent: dict[str, list[LeafSpec]] = defaultdict(list)
    for leaf in leaves:
        by_parent[_parent_path(leaf.code_id)].append(leaf)

    cross_eligible = sorted(
        p for p, ls in by_parent.items() if len(ls) >= thresholds.cross_code_n_siblings
    )
    if not cross_eligible:
        raise RuntimeError(
            f"No parent in framework has >={thresholds.cross_code_n_siblings} "
            "leaves; cannot reserve a cross-code group."
        )
    cross_parent = rng.choice(cross_eligible)
    cross_siblings = sorted(by_parent[cross_parent], key=lambda lf: lf.code_id)[
        : thresholds.cross_code_n_siblings
    ]
    cross_ids = {lf.code_id for lf in cross_siblings}

    other = [lf for lf in leaves if lf.code_id not in cross_ids]
    rng.shuffle(other)

    if len(other) < SPEC_RUMOUR_LEAVES + sum(n for n, _ in SPEC_TIERS):
        raise RuntimeError(
            f"Framework has only {len(leaves)} leaves; gen-specs needs "
            f">={SPEC_RUMOUR_LEAVES + sum(n for n, _ in SPEC_TIERS) + len(cross_ids)}."
        )

    rumour_leaves = other[-SPEC_RUMOUR_LEAVES:]
    other = other[:-SPEC_RUMOUR_LEAVES]

    counts: dict[str, int] = {}
    cursor = 0
    for tier_count, tier_volume in SPEC_TIERS:
        for leaf in other[cursor : cursor + tier_count]:
            counts[leaf.code_id] = tier_volume
        cursor += tier_count
    for leaf in other[cursor:]:
        counts[leaf.code_id] = SPEC_TAIL_FLOOR

    for leaf in cross_siblings:
        counts[leaf.code_id] = SPEC_CROSS_PER_SIBLING_TARGET

    lo, hi = thresholds.rumour_range
    for leaf in rumour_leaves:
        counts[leaf.code_id] = rng.randint(lo, hi)

    # Step 4: hit ``total`` exactly by nudging the largest mutable leaves.
    delta = total - sum(counts.values())
    mutable_ids = [lf.code_id for lf in other[: sum(n for n, _ in SPEC_TIERS)]]
    if not mutable_ids:
        return counts
    mutable_ids.sort(key=lambda c: -counts[c])
    step = 1 if delta > 0 else -1
    i = 0
    while delta != 0 and mutable_ids:
        cid = mutable_ids[i % len(mutable_ids)]
        if step < 0 and counts[cid] <= max(thresholds.min_carrier, SPEC_TAIL_FLOOR):
            mutable_ids.remove(cid)
            continue
        counts[cid] += step
        delta -= step
        i += 1

    return counts


def _sample_metadata(
    leaf: LeafSpec,
    rng: random.Random,
    countries: list[str],
    languages: list[str],
    language_weights: list[float],
    sources: list[str],
    source_weights: list[float],
) -> dict[str, Any]:
    """One record's metadata block — everything except the prose text."""
    country = rng.choice(countries)
    region = rng.choice(COUNTRY_REGIONS[country])
    sensitive = leaf.sensitive_default
    if rng.random() < SENSITIVE_FLIP_PROB:
        sensitive = not sensitive
    return {
        "dataset": "COVID-19",
        "feedback_type": leaf.feedback_type,
        "region": region,
        "country": country,
        "language": rng.choices(languages, weights=language_weights, k=1)[0],
        "source": rng.choices(sources, weights=source_weights, k=1)[0],
        "year": START.year,
        "sensitive": sensitive,
        "codes": leaf.code_id,
    }


def gen_specs(
    framework_path: Path,
    out_path: Path,
    total: int,
    rng: random.Random,
    plot_path: Path | None,
) -> int:
    """Run the gen-specs subcommand: produce a JSONL file of full record specs."""
    logger.info("Loading framework: %s", framework_path)
    leaves = _load_framework(framework_path)
    leaf_by_id = {lf.code_id: lf for lf in leaves}
    logger.info("Framework leaves: %d", len(leaves))

    counts = _allocate_volumes(leaves, total, SPEC_THRESHOLDS, rng)
    logger.info(
        "Allocated %d records across %d leaves (target %d)",
        sum(counts.values()),
        len(counts),
        total,
    )

    languages, language_weights = (
        list(LANGUAGE_DIST),
        list(LANGUAGE_DIST.values()),
    )
    sources, source_weights = list(SOURCE_DIST), list(SOURCE_DIST.values())
    countries = sorted(COUNTRY_REGIONS)

    records: list[dict[str, Any]] = []
    for code_id in sorted(counts):
        leaf = leaf_by_id[code_id]
        for _ in range(counts[code_id]):
            records.append(
                {
                    "id": "",  # filled after shuffle for deterministic IDs
                    "metadata": _sample_metadata(
                        leaf,
                        rng,
                        countries,
                        languages,
                        language_weights,
                        sources,
                        source_weights,
                    ),
                }
            )
    rng.shuffle(records)
    for i, record in enumerate(records, start=1):
        record["id"] = f"doc-{i:04d}"

    carriers = select_carrier_codes(Counter(counts), rng, SPEC_THRESHOLDS)
    logger.info("Carriers:")
    for pattern, leaves_in in carriers.items():
        for cid in leaves_in:
            logger.info("  %-11s %4d  %s", pattern, counts[cid], cid)
    by_pattern = assign_dates(records, carriers, rng)

    # Attach _context so the LLM sees the leaf's name, description, and
    # examples without needing the framework file. The merge step strips
    # _context before writing the final YAML.
    pattern_for_leaf: dict[str, str] = {
        leaf: pattern for pattern, leaves_in in carriers.items() for leaf in leaves_in
    }
    for record in records:
        cid = record["metadata"]["codes"]
        leaf = leaf_by_id[cid]
        record["_context"] = {
            "code_name": leaf.name,
            "code_description": leaf.description,
            "code_examples": leaf.examples[:3],
            "trend_role": pattern_for_leaf.get(cid, "baseline"),
        }

    logger.info("Writing specs JSONL: %s", out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    if plot_path:
        logger.info("Plotting %s", plot_path)
        make_plot(
            by_pattern,
            records,
            carriers,
            plot_path,
            title=f"Planted trends in {out_path.name}",
        )

    return 0


# --- merge: join specs + LLM texts → final YAML ------------------------------

_SENTENCE_SPLIT = re.compile(r"[.!?]+\s+|\n+")


def _approx_sentence_count(text: str) -> int:
    """Quick sentence-count heuristic for the LLM sanity check (not a parser)."""
    if not text.strip():
        return 0
    parts = [p for p in _SENTENCE_SPLIT.split(text.strip()) if p.strip()]
    return max(1, len(parts))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def merge(
    specs_path: Path,
    texts_path: Path,
    yaml_path: Path,
    plot_path: Path | None,
) -> int:
    """Run the merge subcommand: join specs + LLM texts into the final YAML."""
    logger.info("Loading specs: %s", specs_path)
    specs = _load_jsonl(specs_path)
    logger.info("Loaded %d specs", len(specs))

    logger.info("Loading texts: %s", texts_path)
    text_by_id = {obj["id"]: obj for obj in _load_jsonl(texts_path)}
    logger.info("Loaded %d texts", len(text_by_id))

    missing = [s["id"] for s in specs if s["id"] not in text_by_id]
    if missing:
        raise RuntimeError(
            f"{len(missing)} specs missing texts (first 5: {missing[:5]})"
        )
    extras = set(text_by_id) - {s["id"] for s in specs}
    if extras:
        logger.warning("%d texts have no matching spec; ignored.", len(extras))

    sentence_warnings = 0
    out_records: list[dict[str, Any]] = []
    carriers: dict[str, list[str]] = defaultdict(list)
    by_pattern: dict[str, list[int]] = defaultdict(list)
    for i, spec in enumerate(specs):
        text_obj = text_by_id[spec["id"]]
        text = text_obj["text"]
        claimed = int(text_obj.get("sentence_count", _approx_sentence_count(text)))
        if abs(claimed - _approx_sentence_count(text)) > 1:
            sentence_warnings += 1
        # Final record carries ``created`` so the plot reproduces from
        # the YAML alone, and ``sentence_count`` so the existing model schema
        # is satisfied without a second pass.
        metadata = {**spec["metadata"], "sentence_count": claimed}
        out_records.append({"id": spec["id"], "text": text, "metadata": metadata})

        role = spec.get("_context", {}).get("trend_role", "baseline")
        cid = spec["metadata"]["codes"].split(",")[0].strip()
        if role != "baseline" and cid not in carriers[role]:
            carriers[role].append(cid)
        by_pattern[role].append(i)

    if sentence_warnings:
        logger.warning(
            "%d records have sentence_count off by >1 from heuristic; not fatal.",
            sentence_warnings,
        )

    top_comment = build_top_comment(
        dict(carriers), dict(by_pattern), total=len(out_records)
    )
    logger.info("Writing YAML: %s", yaml_path)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(yaml_path, out_records, top_comment)

    if plot_path:
        logger.info("Plotting %s", plot_path)
        make_plot(
            dict(by_pattern),
            out_records,
            dict(carriers),
            plot_path,
            title=f"Planted trends in {yaml_path.name}",
        )

    return 0


# --- plant-in-place: original behaviour --------------------------------------


def plant_in_place(yaml_path: Path, png_path: Path, rng: random.Random) -> int:
    """Add created in place to an existing fixture YAML."""
    logger.info("Loading %s", yaml_path)
    records: list[dict[str, Any]] = yaml.safe_load(yaml_path.read_text())
    logger.info("Loaded %d records", len(records))

    counts: Counter[str] = Counter()
    for r in records:
        counts.update(_record_leaves(r))
    logger.info("Distinct leaf codes: %d", len(counts))

    carriers = select_carrier_codes(counts, rng, DEFAULT_THRESHOLDS)
    logger.info("Chosen carriers:")
    for pattern, leaves in carriers.items():
        for leaf in leaves:
            logger.info("  %-11s %4d  %s", pattern, counts[leaf], leaf)

    by_pattern = assign_dates(records, carriers, rng)
    top_comment = build_top_comment(carriers, by_pattern, total=len(records))

    logger.info("Per-pattern record counts:")
    for pattern in PATTERN_PRIORITY:
        if pattern in by_pattern:
            logger.info("  %-11s %4d", pattern, len(by_pattern[pattern]))

    logger.info("Writing %s", yaml_path)
    write_yaml(yaml_path, records, top_comment)

    logger.info("Plotting %s", png_path)
    make_plot(
        by_pattern,
        records,
        carriers,
        png_path,
        title=f"Planted trends in {yaml_path.name}",
    )

    logger.info("Done.")
    return 0


# --- Main ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("gen-specs", help="Generate spec JSONL (Python-only).")
    p_gen.add_argument(
        "--framework",
        type=Path,
        default=Path("fixtures/coding_framework.json"),
    )
    p_gen.add_argument(
        "--output",
        type=Path,
        default=Path("fixtures/analyze_corpus.specs.jsonl"),
    )
    p_gen.add_argument(
        "--total", type=int, default=SPEC_TOTAL_DEFAULT, help="Target record count."
    )
    p_gen.add_argument(
        "--png",
        type=Path,
        default=None,
        help="Trend-plot path (default: <output>.trends.png).",
    )

    p_merge = sub.add_parser("merge", help="Join specs + LLM texts into final YAML.")
    p_merge.add_argument(
        "--specs",
        type=Path,
        default=Path("fixtures/analyze_corpus.specs.jsonl"),
    )
    p_merge.add_argument(
        "--texts", type=Path, required=True, help="JSONL of {id, text, sentence_count}."
    )
    p_merge.add_argument(
        "--output",
        type=Path,
        default=Path("fixtures/analyze_corpus.yaml"),
    )
    p_merge.add_argument(
        "--png",
        type=Path,
        default=None,
        help="Trend-plot path (default: <output>.trends.png).",
    )

    p_plant = sub.add_parser(
        "plant-in-place", help="Add created to an existing YAML in place."
    )
    p_plant.add_argument(
        "--yaml",
        type=Path,
        default=Path("fixtures/analyze_corpus.yaml"),
    )
    p_plant.add_argument(
        "--png",
        type=Path,
        default=None,
        help="Trend-plot path (default: <yaml>.trends.png).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch to one of the three subcommands."""
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    # PRNG is used purely to generate reproducible synthetic benchmark data;
    # cryptographic strength is not required (and would defeat reproducibility).
    rng = random.Random(args.seed)  # noqa: S311

    if args.cmd == "gen-specs":
        png = args.png or args.output.with_suffix(".trends.png")
        return gen_specs(args.framework, args.output, args.total, rng, png)
    if args.cmd == "merge":
        png = args.png or args.output.with_suffix(".trends.png")
        return merge(args.specs, args.texts, args.output, png)
    if args.cmd == "plant-in-place":
        png = args.png or args.yaml.with_suffix(".trends.png")
        return plant_in_place(args.yaml, png, rng)
    raise AssertionError(f"unknown subcommand {args.cmd!r}")


if __name__ == "__main__":
    sys.exit(main())
