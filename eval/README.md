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

Scores `POST /v1/detect-sensitive` against a Langfuse dataset you name on
the command line, so one script covers every sensitivity dataset.

Set these first. The script reads `.env` at the repo root, so they can
live there.

- `QFA_API_BASE_URL` — the backend to call. Required, with no default, so
  a local run names `http://localhost:8000` itself.
- `QFA_DEV_API_KEY` — a bearer token for that backend. Locally you can set
  `AUTH_API_KEYS` instead, the same JSON the server reads.
- `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for the Langfuse project
  at `LANGFUSE_HOST` that holds the dataset.

```bash
# smoke test against the first 5 records
uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords --limit 5

# full run
uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords
```

| Flag         | Meaning                                                                             |
| ------------ | ----------------------------------------------------------------------------------- |
| `--dataset`  | Langfuse dataset name. Required.                                                    |
| `--limit N`  | Only the first N records. Default: all.                                             |
| `--run-name` | Replaces the generated name, `smoke-<N>-<timestamp>` or `baseline-full-<timestamp>`. |

Each item's `input` is the feedback text, sent to the backend as written,
so pick a dataset that was anonymised before it was uploaded. Each item's
`expected_output` is the human label, `Sensitive` or `Not sensitive`.
Anything else stops the run rather than counting as a wrong answer.

## What a run tells you

Results land in Langfuse as a Dataset Run under `sensitivity-baseline`.

For each record:

| Score                 | Meaning                                                                                                                                             |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `correct`             | 1 when the label matched the human's, 0 when it did not.                                                                                            |
| `classification_case` | `TP` and `TN` are correct. `FP` is a record flagged that should not have been. `FN` is a sensitive record that was missed — filter on it to see what slipped through. |

For the run as a whole:

| Score                                       | The question it answers                                                                                                        |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `accuracy`                                  | Of all records, how many were labelled correctly?                                                                              |
| `recall_sensitive`                          | Of the records that really are sensitive, how many were flagged? Missing one is the costly mistake, so this is the number to watch. |
| `precision_sensitive`                       | Of the records that were flagged, how many really were sensitive?                                                              |
| `f1_sensitive`                              | One number balancing `recall_sensitive` and `precision_sensitive`.                                                             |
| `recall_not_sensitive`                      | Of the records that are not sensitive, how many were correctly left alone?                                                     |
| `n_distinct_sensitivity_types`              | How many different sensitivity types came up. The breakdown per type, split by right and wrong flags, sits in this score's metadata. |
| `mean_sensitivity_types_per_flagged_record` | How many types were assigned to a flagged record on average.                                                                   |

The first five also carry their underlying counts in score metadata.

Records go five at a time, a low score never fails the run, and the
console prints a summary and a link to the run. No workflow runs this
script.
