# `eval/` — LLM-cost-bearing evaluations

This directory holds scripts that score a live endpoint against ground
truth. Each script records its result in
[Langfuse](https://langfuse.510.global/). A script here can also run from
CI, as a manually triggered workflow. Nothing here runs as part of `make
test` or `make lint`. Every run makes real LLM calls, so every run costs
real money.

| Script                    | What it does                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `assign_codes_eval.py`    | Scores `POST /v1/assign-codes` against the Langfuse `assign-codes/ukrain` dataset, per coding level.                |
| `evaluate_sensitivity.py` | Scores `POST /v1/detect-sensitive` against any Langfuse dataset of labelled records, named on the command line.     |
| `upload_prompts.py`       | Checks and uploads the analyze prompts to a Langfuse dataset, e.g. `analyze/prompts-v1`. Makes no LLM call.         |
| `upload_pool.py`          | Checks and uploads the analyze feedback pool to a Langfuse dataset, e.g. `feedback/records-en-v1`. Makes no LLM call. |

## Shared helpers

Call `run_metadata()` in `_common.py`. That is the single source.

| Key                | What it names |
| ------------------ | ------------- |
| `deployed_version` | Package version from `GET /v1/health`. |
| `deployed_commit`  | Git commit the backend reports. Tells two `dev` deploys apart when there is no version bump. |
| `eval_script_sha`  | Commit of the script that sent the requests. |
| `git_branch`       | Branch of that script. |

CI sets `GIT_SHA` / `GIT_REF_NAME` because a CI checkout is detached.
A local run reads `git`.
Start a new script from `_common` (`uv run python eval/x.py`):

```python
from _common import MAX_CONCURRENCY, load_env, resolve_config, run_metadata
```

Local server: set `QFA_API_BASE_URL=http://localhost:8000`, reuse
`AUTH_API_KEYS`, and cap the run with `--smoke-limit`:

```bash
QFA_API_BASE_URL=http://localhost:8000 uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords --smoke-limit 2
```

## Running `assign_codes_eval.py`

Set these before you run the script:

- Set `QFA_DEV_API_KEY` to a bearer token for a reachable QFA backend
  (default: `https://qfa-dev-backend.azurewebsites.net`).
- Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for a Langfuse
  project at `LANGFUSE_HOST` that holds the `assign-codes/ukrain` dataset.

```bash
uv run python eval/assign_codes_eval.py
```

Each dataset item's `input` field already carries the feedback text and
the full coding hierarchy, in `input.hierarchy.coding_levels`. Each item's
`expected_output` field carries the accepted code names per level, in the
form `{"level_1": [...], "level_2": [...], "level_3": [...]}`. The script
needs no local ground-truth file, so it sends one `/v1/assign-codes`
request per item. When the API's top-ranked code at a level matches one
of the accepted names, the script marks that level correct.

The script never fails on a low score. It only reports results, and does
not gate CI. A summary prints to the console. The full per-item and
aggregate results land in Langfuse, as a new Dataset Run under
`assign-codes/ukrain`.

### Running it in CI

The **evaluate-assign-codes** GitHub Actions workflow, in
`.github/workflows/evaluate-assign-codes.yaml`, runs this script against
the same dev backend. The **evaluate** workflow calls it on every run.
To trigger either one, open the Actions tab, select **evaluate** or
**evaluate-assign-codes**, and click Run workflow. Before the first run,
add two repository secrets, `QFA_DEV_API_KEY` and `LANGFUSE_SECRET_KEY`,
and two repository variables, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_HOST`, for the dev environment.

## Running `evaluate_sensitivity.py`

This script scores `POST /v1/detect-sensitive` against a Langfuse dataset
you name on the command line, so the same script works for any
sensitivity dataset.

Set these before you run it. They can live in the repo-root `.env`.
See Shared helpers for a local server.

- Set `QFA_DEV_API_KEY`, or locally `AUTH_API_KEYS`.
- Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for a Langfuse
  project at `LANGFUSE_HOST` that holds the dataset.

```bash
# smoke test against the first 5 records
uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords --smoke-limit 5

# full run
uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords
```

`--dataset` is required. `--smoke-limit N` runs the first N records.
`--run-name` replaces `smoke-<N>-<timestamp>` or `baseline-full-<timestamp>`.

Each dataset item's `input` field is the feedback text, and it is sent to
the backend exactly as written, so choose a dataset that was anonymised
before it was uploaded. Each item's `expected_output` field is the human
label, either `Sensitive` or `Not sensitive`. An item carrying anything
else stops the run, rather than being counted quietly as a wrong answer.

The script never fails on a low score. It only reports results, and does
not gate anything. A summary prints to the console along with a link to
the run, and the full results land in Langfuse as a Dataset Run under the
experiment `sensitivity-baseline`.

Every record there carries two scores. `correct` is 1 when the label
matched the human's and 0 when it did not. `classification_case` says
which kind of outcome it was: `TP` and `TN` are the correct ones, `FP` is
a record that was flagged when it should not have been, and `FN` is a
sensitive record that was missed. Filtering a run on `FN` shows you
everything that slipped through.

The run as a whole carries seven more:

| Score                                       | The question it answers                                                                                                        |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `accuracy`                                  | Of all records, how many were labelled correctly?                                                                              |
| `recall_sensitive`                          | Of the records that really are sensitive, how many were flagged? Missing one is the costly mistake, so this is the number to watch. |
| `precision_sensitive`                       | Of the records that were flagged, how many really were sensitive?                                                              |
| `f1_sensitive`                              | One number balancing `recall_sensitive` and `precision_sensitive`.                                                             |
| `recall_not_sensitive`                      | Of the records that are not sensitive, how many were correctly left alone?                                                     |
| `n_distinct_sensitivity_types`              | How many different sensitivity types came up. The breakdown per type, split by right and wrong flags, sits in this score's metadata. |
| `mean_sensitivity_types_per_flagged_record` | How many types were assigned to a flagged record on average.                                                                   |

The five scores above the two type counts also record the numbers they
were calculated from, so the raw totals behind a percentage are always
one click away.

Records are sent five at a time. Left to itself the Langfuse SDK would
run fifty in parallel, which exhausts the dev backend's small Postgres
connection pool and slows every request down.

### Running it in CI

The **evaluate-sensitivity** workflow, in
`.github/workflows/evaluate-sensitivity.yaml`, runs this script against
the dev backend. Open the Actions tab, select **evaluate-sensitivity**,
and click Run workflow to choose a dataset and start it. Other workflows
can call it too, and must name a dataset when they do.

It reuses the secrets and variables listed above for
`assign_codes_eval.py` and needs nothing of its own. The backend URL is
not among them: CI measures the dev backend, which is the script's
default.

## Running `upload_prompts.py`

This script checks and uploads the working YAML file behind the analyze
prompts dataset. It calls no endpoint, so it makes no LLM call and costs
nothing.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for a Langfuse project
at `LANGFUSE_HOST`, then:

```bash
uv run python eval/upload_prompts.py --dataset analyze/prompts-v1 \
  --input .corpus_work/analyze-pool/prompts-v1.yaml --dry-run

uv run python eval/upload_prompts.py --dataset analyze/prompts-v1 \
  --input .corpus_work/analyze-pool/prompts-v1.yaml
```

`--dry-run` runs every check and reports what would change, but writes
nothing. If an item already exists and its content changed, the script
updates that item. Langfuse keeps the old version.

Each record in the input file is one prompt:

```yaml
- id: P01-en # <prompt_id>-<language>
  prompt: Summarise the main themes and topics raised across these feedback entries, grouped by frequency
  metadata:
    prompt_id: P01
    language: en
    twin_id: null # links to the id of a translation, e.g. P01-es
    family: themes # a known family, or "unplanted"
    use_case: analyze-bulk
    source: QFA training slides v1
    supplied_by: Daan
    supplied_on: "09-09-2026"
```

The script stops before uploading anything if two records share an `id`, if
a `family` is neither a known one nor `unplanted`, or if a record is
missing one of the metadata fields above.

## Running `upload_pool.py`

This script checks and uploads the working YAML file behind the analyze
feedback pool. It calls no endpoint, so it makes no LLM call and costs
nothing. It always uploads to `feedback/records-en-v1`.

Run `scripts/check_pool_masking.py` before uploading, so a planted word
that the anonymizer masks gets reworded first, not discovered later as an
unexplained drop in score.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for a Langfuse project
at `LANGFUSE_HOST`, then:

```bash
uv run python eval/upload_pool.py \
  --input .corpus_work/analyze-pool/records-en-v1.yaml --dry-run

uv run python eval/upload_pool.py \
  --input .corpus_work/analyze-pool/records-en-v1.yaml
```

`--dry-run` runs every check and reports what would change, but writes
nothing. It also prints how many records have no text yet. A real upload
stops if any record has no text or no `review_status`, or if an urgent,
decoy or group record is not `review_status: reviewed`. If an item
already exists and its content changed, the script updates that item.
Langfuse keeps the old version.

Each record in the input file is one feedback record:

```yaml
- id: en-0001 # assigned after a seeded shuffle; carries no label signal
  content: "" # empty means the text is not written yet
  metadata:
    theme: food # the one primary theme; see PLANTED in pool_spec.py
    need: food # null when this record reports no unmet need
    complaint_about: null
    rumour: null
    suggestion: null
    praise_about: null
    info_request: null
    urgent_kind: null # set only on the 6 urgent protection records
    decoy: false # true on the 4 protection decoys
    promise_gap: null
    access_barrier: null
    groups: [] # the vulnerable groups this record is about
    keywords: [] # urgent/decoy records only, filled in alongside the text
    facts: [] # one sentence per fact; filled in alongside the text
    language: en
    created: "2026-07-14T09:23:41Z" # random, seeded, inside 2026-06-01..2026-08-31
    gen_model: null # set when the text is written
    gen_date: null
    review_status: null # "reviewed" or "not_reviewed", set at review time
```

The script stops before uploading anything if an id is not shaped
`en-NNNN` or repeats, a record is missing one of the metadata fields
above, a planted count (`PLANTED` in `pool_spec.py`) is wrong, the decoy
count or the `urgent_kind`s are wrong, a `created` falls outside the
window, or — once a record has text — its length is outside 60–1,200
characters, it has no `facts`, its text uses a banned word (`PSEA`,
`safeguarding`, `exploitation`, `referral`), or an urgent/decoy record
has fewer than 3 `keywords`.

### The vocabulary (`--vocab`)

Add `--vocab <file>` to also check and upload the keyword vocabulary, to
`feedback/vocab-v1`, after the records:

```bash
uv run python eval/upload_pool.py \
  --input .corpus_work/analyze-pool/records-en-v1.yaml \
  --vocab .corpus_work/analyze-pool/vocab-v1.yaml --dry-run
```

A fact counts as reported when one of these keywords appears in a model's
answer, matched by `_common.contains_term` — a whole-word match on text
that `_common.normalize` has lowercased, accent-stripped, and folded
`LGBTQI+` to `lgbtqi`, so `"ill"` never matches inside `"will"`.

Each entry in the vocabulary file is one label value:

```yaml
- kind: theme # or need, complaint_about, rumour, suggestion, praise_about,
  # info_request, promise_gap, access_barrier, or group
  name: food # the label value; "<kind>:<name>" is the bare id, e.g. group:older_people
  keywords:
    en: [food shortage, not enough food, hunger] # 3 or more

- kind: group
  name: older_people
  keywords:
    en: [older person, elderly, older resident]
  issue_keywords: # group entries only, 3 or more
    en: [mobility difficulty, long walk difficult, needs assistance carrying]
```

There is no entry for `urgent_kind` or `decoy`: those keywords stay on the
6 urgent and 4 decoy records themselves (see `upload_pool.py` above), so
the check below can confirm an urgent record's keyword is never found in
a decoy's text.

The script stops before uploading anything if a label value used in the
records has no matching entry, or an entry's value matches no record; an
entry has fewer than 3 `keywords.en`, or a group entry fewer than 3
`issue_keywords.en`; a keyword — in the vocabulary or on a record —
contains a word from `STOPLIST` in `pool_spec.py` (for example `minor`,
which would credit the unaccompanied-minors group for "a minor theme");
two group entries share a keyword; two urgent records share a keyword; or
an urgent record's keyword appears in a decoy's text.
