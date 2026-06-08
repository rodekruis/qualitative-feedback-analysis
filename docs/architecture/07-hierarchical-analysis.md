# Hierarchical analysis (`mode=hierarchical`)

How `POST /v1/analyze` analyses a corpus that is too large for a single LLM
call, without dropping any record.

`single_pass` (the #117 default) sends every record in one prompt and is
guarded by a token cap — input over the cap returns **413 `payload_too_large`**.
`hierarchical` lifts that ceiling: it embeds and clusters the records into
thematically coherent, budget-sized chunks, analyses each chunk (**map**), and
synthesises the partial analyses into one answer (**reduce**), recursing when
the work still overflows the budget.

The two modes are selected by the `mode` request field; the route handler calls
`Orchestrator.analyze` or `Orchestrator.analyze_hierarchical` accordingly — one
orchestrator, one method per mode (see
[ADR-011](../adr/011-drop-orchestrator-port.md)).

## Why an embedding model at all?

Clustering needs a vector per record so that records about the same theme sit
near each other. The model must be **multilingual** — humanitarian feedback
arrives in many languages, and a monolingual model would cluster by language
rather than theme. The model is the only external, swappable dependency in the
path, so it sits behind {py:class}`~qfa.domain.ports.EmbeddingPort`. The choice
of a *self-hosted BGE-M3 ONNX-int8* model over a hosted embedding API — and why
that specific model and format — is recorded in
[ADR-014](../adr/014-embedding-port-and-self-hosted-model.md).

## The pipeline

`Orchestrator.analyze_hierarchical` runs these steps in order:

1. **Availability guard.** If no embedder is configured (`EMBEDDING_*` unset),
   the call raises `AnalysisError` → **502 `analysis_unavailable`**. A
   deployment that never uses `hierarchical` carries no model on disk.
2. **Anonymise first.** Every record's *text* is anonymised, and so is the
   analyst prompt, **before** anything leaves the record — i.e. before embedding
   and before any LLM call. Record *metadata* is left untouched (codes and dates
   are not PII and feed step 3).
3. **Coding-trend table (deterministic, no LLM).**
   {py:func}`~qfa.services.coding_trends.build_coding_trend_table` counts the
   coding labels in the records' metadata per time period, producing a
   code-by-period frequency table. Because it is exact arithmetic over the
   metadata it is fully faithful — it cannot hallucinate. It later anchors the
   reduce step and is returned to the caller as `coding_trends`. The same
   table is also built and returned by `mode=single_pass` — it depends only
   on input metadata, not on the chunking pipeline, so there is no reason to
   gate it on the mode. Granularity (`day` / `week` / `month`, default `week`)
   is configurable via the request body's `period` field, with a server-side
   default from `ANALYZE_DEFAULT_CODING_TREND_PERIOD`.
4. **Embed.** {py:class}`~qfa.domain.ports.EmbeddingPort` turns the anonymised
   texts into dense vectors. Encoding is synchronous CPU-bound computation, not
   I/O — see the port's design rationale in ADR-014.
5. **Cluster into budget chunks.**
   {py:func}`~qfa.services.clustering.cluster_records` runs HDBSCAN over the
   vectors and packs each resulting cluster into one or more chunks. Outliers
   (HDBSCAN's noise label `-1`) become *uncategorised* chunks rather than being
   discarded, and a corpus smaller than `min_cluster_size` is treated as one
   uncategorised group. A coverage invariant is asserted: the chunks partition
   the input exactly — **every record lands in exactly one chunk, none dropped,
   none duplicated**. Why HDBSCAN and not k-means/DBSCAN:
   [ADR-015](../adr/015-hdbscan-clustering.md).

   Two refinements keep the chunks balanced and trend-friendly:

   - **Granularity target.** HDBSCAN clusters are intentionally uneven — one
     dominant theme can hold hundreds of records yet still fit the LLM cap
     whole, becoming a single fat map call whose latency (it runs concurrently
     with the others) sets the wall-clock tail. `ANALYZE_TARGET_CHUNK_TOKENS`
     (default 4000) is a separate, smaller split target: a cluster over it is
     divided into roughly equal contiguous sub-chunks. The effective split
     budget is `min(target, max_total_tokens)`, so the LLM cap is still the hard
     ceiling — the target only ever makes chunks *smaller*. Splitting is
     count-balanced (not greedy fill-then-remainder), so sub-chunks stay
     uniform and concurrency is used evenly.
   - **Date ordering.** Records inside every chunk are sorted by their
     `ANALYZE_CODING_TREND_DATE_FIELD` date (undated records last, stably).
     The map and reduce steps look for trends, so a chunk presented
     chronologically lets the model narrate change over time; and because the
     sort happens *before* the split, each sub-chunk of a big cluster is a
     contiguous time-window rather than an arbitrary slice.
6. **Map.** For each chunk, one analysis LLM call (the same guardrailed envelope
   as `single_pass`, built by `build_map_system_message`) produces a partial.
   Chunks are independent and dominated by LLM round-trip latency, so they are
   mapped **concurrently** (`asyncio.gather`); `gather` preserves chunk order so
   partials stay aligned with their records. Only the partials are on the
   critical path to reduce, so the leaf judge (step 7) is deferred.
7. **Leaf judge (concurrent with reduce).** Each partial is scored by a **leaf
   judge** call for how faithful it is to *its own chunk*. Judging at the leaf is
   deliberate: the top-level synthesis never sees the raw records, so it cannot
   be scored against them — only the leaves can. A failed judge call floors that
   chunk's score at `0.0` and is still counted, so judge failure lowers
   confidence rather than vanishing. The judges feed only the `confidence`, not
   the synthesis, so they are **independent of reduce** and run concurrently with
   it (`asyncio.gather(judge_all, reduce)`).
8. **Reduce (concurrent with the leaf judges).** The partials are synthesised
   into one analysis. If they fit in one reduce call, a single call is made. If
   they overflow, they are split into budget-sized groups, each group is reduced
   to an intermediate, and the reduce **recurses** over the intermediates — a
   tree-reduce. The trend table is attached to the **final** reduce only
   (intermediates pass `None`) so it anchors the top-level answer without being
   double-counted. A convergence safeguard handles the degenerate case where
   every partial overflows on its own: instead of looping forever, it emits one
   reduce call over all partials.

A single semaphore (`ANALYZE_MAX_CONCURRENT_CHUNKS`, default 8) bounds **all**
hierarchical LLM calls — map, leaf judge, and reduce — so total concurrency stays
capped even while the judge and reduce phases overlap; a large corpus therefore
never bursts past the provider's rate limit. Set the cap to `1` for a fully
sequential pipeline.
9. **Confidence.** The per-chunk judge scores are combined into a single
   `confidence` in [0, 1], weighting each chunk by its record count
   (coverage-weighted mean). The lowest single-chunk score is reported as a
   floor in `uncertainty_explanation`, so a small badly-grounded chunk stays
   visible even when the weighted mean is high.
10. **De-anonymise + disclaimer.** As with `analyze`, the synthesis is
   de-anonymised except for `<PERSON_*>` placeholders (see
   [Prompt envelope](06-prompt-envelope.md#selective-de-anonymisation-person-retention)),
   then `ANALYZE_DISCLAIMER` is prepended. The result carries `result`,
   `confidence`, `uncertainty_explanation`, and `coding_trends`.

### Two ways the budget is respected

The token budget is enforced in two distinct places, and it helps to keep them
separate:

- **Map side (splitting).** An over-budget *cluster* is greedily packed into
  several budget-sized chunks during step 5. This is splitting, not recursion.
- **Reduce side (recursion).** An over-budget *set of partials* is tree-reduced
  in step 8. This genuine recursion is what lets the path scale to corpora many
  times the single-call cap.

## Guardrails

The prompt-injection guardrails from [the prompt envelope](06-prompt-envelope.md)
are applied at **both** the map and the reduce prompts (`build_map_system_message`
and `build_reduce_system_message`), so untrusted record text is treated as data
at every LLM hop, not only the first.

## Flow

```mermaid
flowchart TD
    start([analyze_hierarchical]) --> guard{embedder<br/>configured?}
    guard -- no --> err[AnalysisError → 502]
    guard -- yes --> anon[Anonymise record texts + prompt]
    anon --> trend[Build coding-trend table<br/>deterministic, no LLM]
    trend --> embed[Embed texts → vectors<br/>EmbeddingPort]
    embed --> cluster[HDBSCAN + balanced budget packing<br/>date-sorted, ~equal chunks<br/>cover every record]
    cluster --> map[MAP per chunk concurrently:<br/>analysis call only<br/>bounded by max_concurrent_chunks]
    map --> judge[Leaf-judge each partial<br/>concurrent with reduce]
    map --> fits{partials fit<br/>one reduce call?}
    fits -- yes --> synth[Single reduce call<br/>+ trend table]
    fits -- no --> tree[Group to budget, reduce each,<br/>recurse over intermediates]
    tree --> synth
    judge --> conf[Coverage-weighted confidence<br/>+ min-score floor]
    synth --> conf
    conf --> deanon[De-anonymise except PERSON<br/>+ prepend disclaimer]
    deanon --> out([AnalysisResultModel:<br/>result, confidence,<br/>uncertainty, coding_trends])
```

## Sequence summary

```mermaid
sequenceDiagram
    participant route as Route handler
    participant orch as Orchestrator
    participant anon as AnonymizationPort
    participant emb as EmbeddingPort
    participant llm as LLMPort

    route->>orch: analyze_hierarchical(request, deadline)
    orch->>anon: anonymize(each record text, prompt)
    anon-->>orch: anonymised texts + mapping
    orch->>orch: build_coding_trend_table(metadata)
    orch->>emb: embed(anonymised texts)
    emb-->>orch: dense vectors
    orch->>orch: cluster_records(records, vectors) -> chunks
    loop per chunk (MAP — concurrent, analysis only)
        orch->>llm: complete(map system msg, chunk records)
        llm-->>orch: partial analysis
    end
    note over orch,llm: leaf JUDGE and REDUCE run concurrently (one shared semaphore caps total in-flight ≤ max_concurrent_chunks)
    par leaf judge per chunk
        orch->>llm: complete(leaf judge msg, partial)
        llm-->>orch: faithfulness score
    and reduce (tree-reduce on overflow)
        orch->>llm: complete(reduce system msg, partials + trend table on final)
        llm-->>orch: synthesis
    end
    orch->>orch: coverage-weighted confidence + floor
    orch->>anon: deanonymize(synthesis, mapping minus PERSON)
    anon-->>orch: partially de-anonymised text
    orch-->>route: AnalysisResultModel(result, confidence, uncertainty, coding_trends)
```
