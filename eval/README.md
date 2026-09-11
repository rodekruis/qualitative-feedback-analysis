# `eval/` — LLM-cost-bearing evaluations

This directory holds scripts that score a live endpoint against ground
truth. Each script records its result in
[Langfuse](https://langfuse.510.global/). A script here can also run from
CI, as a manually triggered workflow. Nothing here runs as part of `make
test` or `make lint`. Every run makes real LLM calls, so every run costs
real money.

| Script                 | What it does                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `assign_codes_eval.py` | Scores `POST /v1/assign-codes` against the Langfuse `assign-codes/ukrain` dataset, per coding level. |

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

## Running it in CI

The **evaluate** GitHub Actions workflow, in
`.github/workflows/evaluate.yaml`, runs this script against the same dev
backend. To trigger it, open the Actions tab, select **evaluate**, and
click Run workflow. Before the first run, add three repository secrets:
`QFA_DEV_API_KEY`, `LANGFUSE_PUBLIC_KEY`, and `LANGFUSE_SECRET_KEY`.
