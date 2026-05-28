# Prompt: generate a richer corpus for trend-planting

This prompt is intended to be fed to a strong general-purpose LLM (Claude /
GPT-4-class) **together with `fixtures/coding_framework.json` as an
attachment** to produce a denser drop-in replacement for
`fixtures/analyze_corpus.yaml`. The result is then handed to
`scripts/plant_trends_in_corpus.py`, which injects creation dates and the
top-of-file benchmark block.

The current fixture (1000 records, max ~29 records on the busiest leaf code)
makes single-code patterns — emerging, declining, step — statistically marginal
because per-month counts are dominated by Poisson noise. Use this prompt when
you want clean signals on every planted pattern.

---

## Prompt (paste this verbatim to the LLM, with the coding framework attached)

You are generating a **synthetic, labelled benchmark corpus** of COVID-19
community-feedback records for testing trend-detection on a hierarchical-analysis
backend. The output must be a single YAML file in the schema below.

### Schema (must match `qfa.domain.models.FeedbackRecordModel`)

Each record is a YAML mapping:

```yaml
- id: doc-NNNN              # zero-padded sequential, starting at doc-0001
  text: "2-4 sentences..."  # community feedback, first-person or community voice
  metadata:
    dataset: COVID-19
    feedback_type: <one of: Observation, perception or belief | Encouragement or praise | Request or suggestion | Question | Rumour or hearsay>
    region: <plausible region name>
    country: <plausible country name>
    language: <ISO 639-1 lower, e.g. en, fr, es>
    source: <community feedback session | focus group | hotline | survey | volunteer report>
    year: 2020                              # ALL records in calendar year 2020
    sentence_count: <int matching `text`>
    sensitive: <bool>
    codes: <single colon-hierarchical leaf path from coding_framework.json, OR a comma-separated list of such paths>
```

Metadata values are restricted to **`str | int | float | bool`** only — no
nested structures, no datetimes.

### Volume targets

- **Total: 5000 records.**
- **Per-code targets** (codes are from the attached `coding_framework.json`):
  - Top 5 leaf codes: **≥ 120 records each.**
  - Next 25 leaf codes: **≥ 50 records each.**
  - Coverage: use ≥ 100 distinct leaf codes overall, but concentrate volume on
    the top tier so single-code trends are statistically detectable.
- One parent node must have **≥ 3 sibling leaves each with ≥ 40 records** (for
  cross-code trend testing).
- Include **3-5 low-volume codes with 4-10 records each** (for rumour-style
  outlier planting).

### Content quality

- Records must be **realistic community feedback** in the COVID-era humanitarian
  context (Sierra Leone, DRC, Senegal, Liberia, Burkina Faso, etc.).
  2-4 sentences. First-person or quoted community voice.
- **No PII.** Use generic role labels ("a community elder", "my neighbour") —
  never proper names.
- Vary `language` across `en` (~70 %), `fr` (~20 %), `es` (~5 %), plus a small
  tail in `uk`, `ru`, `xx` (matches Presidio's configured languages).
- Vary `region` / `country` / `source` realistically.
- `sentence_count` must equal the actual number of sentences in `text`.
- `sensitive: true` for ~3 % of records (mentions of death, identifiable
  household illness, etc.).
- `codes` must be exact paths from the framework — **do not invent codes.**

### What you do NOT do

- Do **not** add a `creation_date` field. A separate helper script
  (`scripts/plant_trends_in_corpus.py`) injects designed creation dates and
  the top-of-file benchmark block. Your job is to produce the raw record
  corpus.
- Do **not** include any preamble, commentary, or YAML comments. Output the
  bare YAML list (`- id: ...` repeated) and nothing else.

### Self-check before emitting

- Each record validates against the schema above.
- Per-code counts match the volume targets (mental tally on the top codes).
- At least one parent has 3 sibling leaves with ≥ 40 records each — name them
  in a final tally line **after** the YAML, then delete that line before
  final output.

Begin emitting the YAML now.
