# `scripts/` — operator runbooks

Standalone helpers, run manually (not in CI). Each one has a top-of-file
docstring with full reference; this README is the workflow for the script
people ask about most: regenerating the `analyze_corpus.yaml` fixture.

| Script                          | What it does                                                     |
| ------------------------------- | ---------------------------------------------------------------- |
| `generate_corpus.py`            | Build / regenerate `fixtures/analyze_corpus.yaml` (see below).   |
| `generate_corpus.prompt.md`     | LLM prompt that fills in `text` during corpus generation.        |
| `translate_csv_uk_en.py`        | One-off translation helper for `fixtures/confidential/`.         |
| `update_auth_api_keys.py`       | Rotate API keys against a running instance.                      |
| `test-api.sh`                   | Curl smoke checks against a running instance.                    |
| `espo_crm/`                     | Adapters / utilities for the EspoCRM integration.                |

---

## Regenerating `fixtures/analyze_corpus.yaml`

`fixtures/analyze_corpus.yaml` is the trend-detection benchmark used by
`POST /v1/analyze` with `mode=hierarchical`. Every record carries a known
`creation_date` and a fraction of records sit on auto-selected "carrier"
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
│     creation_dates. One JSON object per record, no `text` yet.      │
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
    "creation_date": "2020-10-17"
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
existing subscription. The recipe Claude follows in the session:

1. Read `scripts/generate_corpus.prompt.md` once.
2. Slice `fixtures/analyze_corpus.specs.jsonl` into batches of 50–100
   records. (Smaller = safer per call, larger = fewer calls; 100 is the
   sweet spot.)
3. For each batch: paste the prompt + the batch JSON into a fresh message,
   collect the returned `texts` array, append each element as one line to
   `texts.jsonl`.
4. When all batches are done, sanity-check that `wc -l texts.jsonl` matches
   the spec count.

A 5000-record corpus runs in ~50 batches. The most common failure mode is
the LLM returning a malformed JSON array — the prompt's self-check
section asks the model to validate before emitting; if a batch still comes
back broken, regenerate just that batch rather than the whole run.

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
dump) and you want to retro-fit it with planted `creation_date`s for the
same trend-detection benchmark. It re-uses the existing `codes` field and
only mutates `metadata.creation_date` + `metadata.year`. The carrier
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
