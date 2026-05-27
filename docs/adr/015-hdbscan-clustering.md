# ADR-015: Cluster feedback records with HDBSCAN

## Status

Accepted

## Context

`mode: "hierarchical"` (#124, see
[ADR-014](014-embedding-port-and-self-hosted-model.md)) groups feedback records
into thematically coherent chunks before a map-reduce over the LLM, so each map
call sees related records and the synthesis is built from coherent partials.
Grouping runs on the embedding vectors, **per request, on CPU**, over a corpus
whose size and number of distinct themes are **unknown ahead of time**. Records
that belong to no clear theme must not be lost.

Two correctness invariants are owned by the clustering step regardless of the
algorithm (asserted in {py:func}`~qfa.services.clustering.cluster_records`):
every record lands in exactly one chunk (**full coverage**), and no chunk
exceeds the token budget (**budget**). Clustering *quality* therefore affects
insight quality, not correctness — even a poor clustering still analyses every
record exactly once.

## Decision

Cluster the dense vectors with **HDBSCAN** (`hdbscan.HDBSCAN`):

1. `min_cluster_size` is configurable (`ORCHESTRATOR_MIN_CLUSTER_SIZE`,
   default 5) and the distance metric is configurable
   (`ORCHESTRATOR_CLUSTERING_METRIC`, default `euclidean`).
2. HDBSCAN's noise label (`-1`) maps to **uncategorised** chunks — those records
   are still analysed, never dropped.
3. A corpus smaller than `min_cluster_size` skips clustering and is treated as a
   single uncategorised group (HDBSCAN cannot form a cluster below that size).
4. Clustering lives in `qfa.services.clustering` as deterministic logic with
   **no port** — it has no external dependency to swap (see ADR-014, point 6).

## Options considered

### A. HDBSCAN (chosen)

- **Discovers the number of clusters** from the data — we never guess *k*. The
  count of distinct themes in a corpus is exactly what we don't know up front.
- **No global radius to tune** — its hierarchical density model copes with
  themes of differing tightness, unlike a single DBSCAN `eps`.
- **First-class noise label** — off-topic or idiosyncratic records are labelled
  `-1` instead of being forced into the nearest cluster; we route them to
  uncategorised chunks so they are still analysed.
- **Con:** heavier than k-means and exposes a `min_cluster_size` knob whose best
  value is corpus-dependent (flagged for tuning against real data). Acceptable —
  corpora are request-bounded and clustered once per request on CPU.

### B. k-means — rejected

Requires a preset *k*; forces every point into a cluster (no noise concept), so
outliers drag the centroids; and assumes roughly spherical, equal-size clusters
that feedback themes do not follow.

### C. DBSCAN — rejected

Has the noise concept but needs a single global `eps`; one radius rarely fits
themes of varying density, and tuning it per request is impractical. HDBSCAN is
essentially DBSCAN with the density threshold made hierarchical and automatic.

## Consequences

- A new CPU-only runtime dependency, `hdbscan` (with its numpy/scikit build).
- `min_cluster_size` and the metric are operational tunables; defaults are
  conservative and documented in the settings reference. Real-corpus tuning of
  `min_cluster_size` is a follow-up.
- Because correctness rests on the coverage + budget invariants asserted in
  code, swapping the algorithm later is a localised change in
  `qfa.services.clustering` with no ripple into the orchestrator.

## Participants

Marius
