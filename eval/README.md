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

This script takes its dataset as an argument instead of hard-coding one,
so the same script serves every sensitivity dataset in Langfuse.

Set these before you run it:

- `QFA_API_BASE_URL` — the backend to call. Required, with no default, so
  a local run has to name `http://localhost:8000` itself.
- `QFA_DEV_API_KEY` — a bearer token for that backend. A local run can
  instead set `AUTH_API_KEYS` to the same JSON the server reads; the
  script takes the first entry that still holds a plaintext `key`.
- `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` for the Langfuse
  project at `LANGFUSE_HOST` that holds the dataset.

The script loads `.env` from the repo root, so these can live there.

```bash
# smoke test against the first 5 records
uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords --limit 5

# full run
uv run python eval/evaluate_sensitivity.py \
  --dataset sensitivity/SubsetIFRCBorderlineSensitiveRecords
```

| Flag         | Meaning                                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------------------------- |
| `--dataset`  | Langfuse dataset name. Required.                                                                              |
| `--limit N`  | Only the first N records. Default: all of them.                                                               |
| `--run-name` | Replaces the generated name: `smoke-<N>-<timestamp>` with `--limit`, otherwise `baseline-full-<timestamp>`.    |

Each dataset item's `input` field is the raw feedback text. Its
`expected_output` field is the human label, either the string `Sensitive`
or `Not sensitive`. Any other label stops the run instead of scoring as a
miss, so a mislabelled dataset shows up as an error rather than a bad
score.

Results land in Langfuse under the experiment `sensitivity-baseline`, as a
Dataset Run:

- Per record, `correct` (1 or 0) and `classification_case` (`TP`, `TN`,
  `FP`, `FN`), so a run can be filtered down to only its false positives.
- Per run, `accuracy`, `precision_sensitive`, `recall_sensitive`,
  `specificity_not_sensitive` and `f1_sensitive`. Each carries the raw
  TP/TN/FP/FN counts in its score metadata.
- Per run, `n_distinct_sensitivity_types` and
  `mean_sensitivity_types_per_sensitive_record`, which show which
  sensitivity types drive the sensitive predictions, counted separately
  for true and false positives. The full distribution sits in the first
  score's metadata.

Records are sent 5 at a time. Like the other script, this one never fails
on a low score: the console gets a summary and a link to the Langfuse run.

No workflow runs this script. Run it locally.