# ADR-020: `mistral-medium-3-5` as the judge model

## Status

Accepted

## Context

#258 made the judge connection configurable independently of the generation
model (`JUDGE_LLM_*`, [settings reference](../operations/settings-reference.md)),
but left it unset everywhere: judge calls ran on the primary model,
`azure/gpt-5.4`, in every deployed environment. #259 is the follow-up
decision — which model the judge connection should actually point at.

The product owner directed `mistral-medium-3-5`, already deployed on the
existing Azure AI Foundry resource. Per `src/qfa/resources/model_prices.yaml`,
it is materially cheaper than the incumbent: $1.50/1M input, $7.50/1M output,
versus $2.50/1M and $15.00/1M for `azure/gpt-5.4` — roughly 1.7× cheaper on
input, 2× cheaper on output. Judge calls happen at four call sites, one judge per
generation call on the `analyze` and `summarize` paths, so this is a
material share of total LLM spend, not a rounding error.

## Decision

`azure_ai/mistral-medium-3-5`, on the existing Azure Foundry resource, serves
all four judge call sites in every deployed environment: the `analyze` judge,
the hierarchical leaf judge, and the judges in `summarize` and
`summarize_bulk`. Configured via `JUDGE_LLM_MODEL` + `JUDGE_LLM_API_BASE`
(`var.judge_llm_model`, `var.judge_llm_api_base` in `infra/variables.tf`), no
new credential — the judge connection inherits `LLM_API_KEY` from the primary
connection, same Azure Foundry resource, same trust boundary.

## How it was verified

**Not yet run.** This decision is shipped without live evaluation evidence:
no automated evaluation harness exists in this repo, and no environment
available at implementation time held Azure credentials to run one against
the real deployment. The rollout below is staged specifically to catch a
bad judge model with limited blast radius in the absence of that evidence,
and the rollback conditions are stated as objective log/metric signals a
human can act on without needing a benchmark.

## Evidence

<!-- PENDING: no evaluation harness exists yet. If one is built later
(e.g. for the follow-up issue on the two free-text judge sites, or a
dedicated evaluation ticket), its output belongs here. Until then, this
decision rests on the cost case in Context and the staged rollout below,
not on a discrimination or agreement benchmark. -->

## Limitations

- No offline or live evidence that `mistral-medium-3-5` discriminates good
  judge inputs from bad ones as reliably as `azure/gpt-5.4` did. The staged
  rollout and rollback conditions below are the mitigation for shipping
  without that evidence, not a substitute for it.
- The two free-text judge sites (`summarize_bulk`, `summarize`) parse a bare
  float and raise `AnalysisError` on anything else — a weaker or
  differently-tuned model is more likely to break that contract than the
  two structured sites. See the follow-up issue below.

## Rollout

dev → staging → prd, one environment at a time via the per-environment
`AZ_JUDGE_LLM_API_BASE` GitHub Actions variable and a `terraform apply`,
each soaking at least one working day before promoting to the next
environment.

## Rollback

Set the environment's `AZ_JUDGE_LLM_MODEL` GitHub Actions variable (and
`AZ_JUDGE_LLM_API_BASE`, if set) to `""` and re-apply. Because `JUDGE_LLM_MODEL`
unset means "use the primary client" (`src/qfa/settings.py`), rollback is a
config change with **no code change and no redeploy of the image** — the
cheapest rollback the `JUDGE_LLM_*` mechanism allows.

## Rollback conditions

Stated as observables, not feelings:

1. Any `AnalysisError` with `"LLM judge returned invalid quality score"` in
   the logs — a failed `/v1/summarize` or `/v1/summarize-bulk` request
   caused by the judge, at a site that is supposed to degrade, not fail.
2. A sustained rise in the 5xx alert on the summarise routes
   (`infra/observability.tf`).
3. A sustained rise in `"Analyse judge call failed"` warnings, i.e.
   `quality_score` coming back `null` on `/v1/analyze`.
4. `SELECT model, count(*), sum(cost_usd) FROM llm_calls WHERE model =
   'azure_ai/mistral-medium-3-5' GROUP BY model` returning `sum(cost_usd) =
   0` — pricing didn't register and cost attribution is silently broken.
   `tests/scripts/test_infra_judge_config.py` guards this at build time;
   this catches a deployment-time mismatch it can't see (e.g. a Foundry
   deployment renamed after this ADR shipped).

## When to revisit

- Any rollback condition above fires.
- An evaluation harness is built and produces evidence that contradicts
  this decision.
- The follow-up issue below lands and changes the failure mode at the two
  free-text sites.

## Follow-up

Opened as #299: the two free-text judge sites (`summarize.py`'s
`summarize_bulk` and `summarize`) parse a bare float and raise
`AnalysisError` on anything else, unlike the two structured `analyze`
judge sites which degrade to `quality_score=None`. Converting them to a
structured `response_model` is proven feasible against Azure AI Mistral by
`_provider_safe_response_format` (`src/qfa/adapters/llm_client.py`). Not
done in this change.

## Participants

Olaf
