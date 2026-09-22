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

Each run queries the backend's `/v1/health` endpoint for the deployed
package version and git commit. The script records both, as
`deployed_version` and `deployed_commit`, in the run metadata and in the
run name. These values name the code that answered the requests.
`deployed_commit` is the one that actually tells two deploys apart:
`build-from-commit.yaml` can push a commit to `dev` with no version bump
at all, so `deployed_version` alone cannot tell such deploys apart.

The script also records its own commit SHA and branch, as
`eval_script_sha` and `git_branch`. This value names the code that sent
the requests. When the backend does not yet run the latest merge, the two
values can differ.

In CI, `eval_script_sha` and `git_branch` come from the GitHub Actions
context (`GIT_SHA`, `GIT_REF_NAME`). A local run reads them from `git`
directly, so they show up even without those two variables set.

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

Set these before you run it. The script reads `.env` from the repo root,
so they can live there rather than in your shell.

- Set `QFA_API_BASE_URL` to the backend you want to measure. It defaults
  to the dev backend, so a local run against `http://localhost:8000` has
  to name it.
- Set `QFA_DEV_API_KEY` to a bearer token for that backend, or, locally,
  set `AUTH_API_KEYS` to the same JSON the server reads.
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

`--dataset` is the only required flag. `--smoke-limit N` runs just the first N
records, which is how you check a change before paying for a full run,
and `--run-name` replaces the name the script generates itself, either
`smoke-<N>-<timestamp>` or `baseline-full-<timestamp>`.

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
