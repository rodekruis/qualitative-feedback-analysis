# `scripts/` — operator runbooks

Standalone helpers, run manually (not in CI). Each one has a top-of-file
docstring with full reference; this README is the workflow for the script
people ask about most: regenerating the `analyze_corpus.yaml` fixture.

| Script                          | What it does                                                     |
| ------------------------------- | ---------------------------------------------------------------- |
| `fetch_embedding_model.py`      | Download the BGE-M3 ONNX embedder to a gitignored local path (dev). |
| `generate_corpus.py`            | Build / regenerate `fixtures/analyze_corpus.yaml` (see below).   |
| `generate_corpus.prompt.md`     | LLM prompt that fills in `text` during corpus generation.        |
| `stress_analyze.py`             | Drive `POST /v1/analyze` with a seeded corpus sample, single-call or in parallel (see below). |
| `translate_csv_uk_en.py`        | One-off translation helper for `fixtures/confidential/`.         |
| `update_auth_api_keys.py`       | Rotate API keys against a running instance.                      |
| `test-api.sh`                   | Curl smoke checks against a running instance.                    |
| `espo_crm/`                     | Adapters / utilities for the EspoCRM integration.                |

For interactive in-process exploration of the same corpus (no server,
direct `Orchestrator.analyze_hierarchical` calls, results displayed
inline) see [`notebooks/analyze_corpus.ipynb`](../notebooks/analyze_corpus.ipynb). It uses
{py:func}`qfa.api.composition.build_orchestrator` to assemble the
domain object graph against the live LLM.

---

## Regenerating `fixtures/analyze_corpus.yaml`

`fixtures/analyze_corpus.yaml` is the trend-detection benchmark used by
`POST /v1/analyze` with `mode=hierarchical`. Every record carries a known
`created` and a fraction of records sit on auto-selected "carrier"
codes whose dates trace a designed temporal pattern (spike, emerging,
declining, step, cross-code, rumour). The detector's job is to recover
those patterns; the fixture's job is to give it a ground truth to be
scored against.

The fixture is built in two halves so that *statistics stay in Python* and
*creative text stays in the LLM*:

```text
┌──────────────────────────────────────────────────────────────────────┐
│  1. gen-specs (Python)                                              │
│     fixtures/coding_framework.json  ──►  analyze_corpus.specs.jsonl │
│     Allocates leaf-code volumes, samples metadata, plants trend     │
│     created dates. One JSON object per record, no `text` yet.       │
├──────────────────────────────────────────────────────────────────────┤
│  2. LLM batches (Claude Code)                                       │
│     analyze_corpus.specs.jsonl  ──►  texts.jsonl                    │
│     Slice into batches; each batch is fed to `generate_corpus.      │
│     prompt.md` plus `fixtures/coding_framework.json`. LLM returns   │
│     `{id, text, sentence_count}` per record.                        │
├──────────────────────────────────────────────────────────────────────┤
│  3. merge (Python)                                                  │
│     specs.jsonl + texts.jsonl  ──►  analyze_corpus.yaml + .png      │
│     Joins by id, validates coverage, writes final YAML with the     │
│     planted-trend comment block, re-renders the trend plot.         │
└──────────────────────────────────────────────────────────────────────┘
```

The Python halves are deterministic given a seed; the LLM half is the only
non-determinism. Trend shape is decided in step 1 and the LLM cannot
disturb it.

### Step 1 — generate the specs

```bash
uv run python scripts/generate_corpus.py gen-specs \
    --output fixtures/analyze_corpus.specs.jsonl
```

Default: 5000 records across ~104 leaf codes. Override with `--total N`
and `--seed N` if you need a different run. The script also writes
`fixtures/analyze_corpus.specs.trends.png` so you can eyeball the planted
trends before committing to the expensive LLM step.

A spec looks like:

```json
{
  "id": "doc-0001",
  "metadata": {
    "dataset": "COVID-19",
    "feedback_type": "Observation, perception or belief",
    "region": "Kenema District",
    "country": "Sierra Leone",
    "language": "en",
    "source": "hotline",
    "year": 2020,
    "sensitive": false,
    "codes": "covid-19:observation-perception-or-belief:beliefs-about-treatment-for-the-disease:beliefs-about-use-of-herbs-or-other-natural-materials-for-treatment",
    "created": "2020-10-17"
  },
  "_context": {
    "code_name": "Beliefs about use of herbs or other natural materials for treatment",
    "code_description": "Statements about the use or consumption of herbs ...",
    "code_examples": ["...", "...", "..."],
    "trend_role": "cross_code"
  }
}
```

`_context` is for the LLM (so it knows the leaf's name, definition, and a
few example utterances) and is stripped by step 3. `trend_role` is one of
`spike | emerging | declining | step | cross_code | rumour | baseline`; the
LLM is told never to surface it in prose.

### Step 2 — write the prose with an LLM

Drive the batches from a Claude Code session — that keeps the work on the
existing subscription. To kick it off, open a session in the repo and
say something like:

> Read `scripts/generate_corpus.prompt.md` and run it.

The prompt file is self-contained: its top half is *driver instructions*
for the orchestrating Claude (count specs, slice into batches of ~100,
send each batch through the per-batch prompt, append to `texts.jsonl`,
validate counts at the end), and its bottom half is the *per-batch
prompt* the driver copies into each LLM call. You do not need to point
Claude at this README — the prompt knows the full step-2 workflow.

A 5000-record corpus runs in ~50 batches. The most common failure mode is
the LLM returning a malformed JSON array — the per-batch prompt's
self-check section asks the model to validate before emitting; if a
batch still comes back broken, the driver regenerates just that batch
rather than the whole run. Crashes mid-run are recoverable because
`texts.jsonl` is append-only and ids already present are skipped on
resume.

### Step 3 — merge into the final YAML

```bash
uv run python scripts/generate_corpus.py merge \
    --specs fixtures/analyze_corpus.specs.jsonl \
    --texts texts.jsonl \
    --output fixtures/analyze_corpus.yaml
```

The merge step:

- joins on `id` (errors if any spec is missing a text);
- validates each text's `sentence_count` against a simple heuristic
  (warns if off by >1, never fatal);
- strips `_context`;
- prepends the planted-trend benchmark comment block to the YAML;
- re-renders the trend-plot PNG next to the YAML.

After merging, the YAML is ready to commit. The intermediate
`specs.jsonl` and `texts.jsonl` are throwaway — they're regenerable
from the seed and the prompt, so leave them out of git.

### `plant-in-place` — the third subcommand

```bash
uv run python scripts/generate_corpus.py plant-in-place \
    --yaml fixtures/some_existing_corpus.yaml
```

Use this when you have a corpus that *already has prose* (e.g. a real-world
dump) and you want to retro-fit it with planted `created`s for the
same trend-detection benchmark. It re-uses the existing `codes` field and
only mutates `metadata.created` + `metadata.year`. The carrier
thresholds default to the values calibrated for the original 1000-record
fixture; that's deliberate — `gen-specs` uses stronger thresholds because
its corpus is denser.

---

## Reproducibility

All three subcommands take `--seed N` (default 42). Same seed + same
framework JSON + same code = same specs and same carrier picks, every
time. The LLM step is the only point of non-determinism in the pipeline;
re-running it with a different model or temperature will change the prose
but not the trend shape.

## Why not have Python write the prose too?

Tried it. Templated text from Python produces a corpus that looks "real"
to character-count metrics but trivially clusters because every record
on the same code shares an n-gram backbone. The downstream BGE-M3
embedder collapses near-duplicates and the trend-detector then sees
phantom signal from the repetition rather than from the planted dates.
LLM-written prose gives genuinely independent sentences that vary across
the same code — which is what every real-world corpus does and what the
detector needs to handle.


---

## Driving `/v1/analyze` with `stress_analyze.py`

`scripts/stress_analyze.py` posts samples from
`fixtures/analyze_corpus.yaml` to a running API instance — either one
request at a time (quality smoke test) or fanned out in parallel
(stress test). Both use the **production vector**: real HTTP, real
auth, real serialization. For interactive in-process inspection,
use `notebooks/analyze_corpus.ipynb` instead.

### Prerequisites

- A server reachable at `--base-url` (default `http://localhost:8000`)
  with `mode=hierarchical` available — i.e. `EMBEDDING_MODEL_PATH`
  set on the server side. Don't have the model locally? Run
  `uv run python scripts/fetch_embedding_model.py` and paste the
  printed `EMBEDDING_*` lines into the server's environment.
- `AUTH_API_KEYS` set in the shell where you run the script (same
  JSON the server reads), or pass `--api-key` explicitly. The first
  key entry is used as the Bearer token.

### Quality smoke test (single request)

```
uv run python scripts/stress_analyze.py --limit 50 --seed 42
```

Fires one request with 50 records and prints a summary. The raw
response lands in `.corpus_work/stress_<UTC-ts>.jsonl` — open it with
`jq` or load it from a notebook to inspect `analysis`, `quality_score`,
`confidence`, and `coding_trends`.

### Stress test (parallel requests)

```
uv run python scripts/stress_analyze.py \
    --limit 100 --seed 42 \
    --concurrency 5 --total-calls 20
```

Maintains up to 5 in-flight requests until 20 have completed. Prints
p50 / p95 / p99 latency, status-code distribution, and writes raw
results to `.corpus_work/`.

### Reproducibility

`--seed` controls which records get sampled — same seed picks the same
subset across runs, so latency comparisons are apples-to-apples. The
LLM itself is non-deterministic, so the analysis text will vary.

### Why a separate notebook *and* a script?

Two artefacts because the use cases have different shapes:

- **Notebook** (`notebooks/analyze_corpus.ipynb`) — one analysis,
  fully inspected, interactive iteration, no HTTP. Direct call to
  `Orchestrator.analyze_hierarchical` via
  {py:func}`qfa.api.composition.build_orchestrator`.
- **Script** (this one) — many analyses, only aggregate metrics
  matter, parallel, exercises the real REST/auth/validation path.

They share the corpus loader (`load_sample`) — the notebook imports
it from this script.
