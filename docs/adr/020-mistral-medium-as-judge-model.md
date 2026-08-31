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
input, 2× cheaper on output. Judge calls happen at five call sites, one judge per
generation call on the `analyze` and `summarize` paths, so this is a
material share of total LLM spend, not a rounding error. The cost argument only
strengthened afterwards: #310 extended the split to the `assign_codes`
per-level judge, which fires once per level per selected path, so a larger
share of call volume moves onto the cheaper model than when this ADR was
written.

## Decision

`azure_ai/mistral-medium-3-5`, on the existing Azure Foundry resource, serves
all five judge call sites in every deployed environment: the `analyze` judge,
the hierarchical leaf judge, the judges in `summarize` and `summarize_bulk`,
and the per-level judge in `assign_codes` (added by #310 after this ADR was
accepted — #258's exclusion of the coding path was scope-only, no
coding-specific concern was ever recorded). Configured via `JUDGE_LLM_MODEL`
+ `JUDGE_LLM_API_BASE` (`var.judge_llm_model`, `var.judge_llm_api_base` in
`infra/variables.tf`), no
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
- The `assign_codes` per-level judge has **no degradation path**: an
  out-of-range score raises `AnalysisError` and an unparseable structured
  response raises `LLMResponseParseError`, either of which fails the
  `/v1/assign-codes` request — unlike the two `analyze` judges, which fall
  back to `quality_score=None`. Same theme as #299.
- That call site issues `complete()` without a `timeout`, so it runs on
  `LiteLLMClient`'s default per-attempt budget rather than a deadline-derived
  one. Pre-existing, but the budget now applies to a different model.

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
4. Any `AnalysisError` with `"LLM judge returned score outside 0.0-1.0"` in
   the logs, or a sustained rise in the 5xx alert on `/v1/assign-codes`
   (`infra/observability.tf`) — the coding judge fails the request rather
   than degrading.
5. `SELECT model, count(*), sum(cost_usd) FROM llm_calls WHERE model =
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
- **Rollback condition 3 fired in dev-test on 2026-08-31** (#309): the judge
  call 400s against `azure_ai/mistral-medium-3-5`, so every `/v1/analyze-bulk`
  confidence score is `null` while the judge connection is active. The cause is
  not yet confirmed; the lever if it needs pulling is the config rollback above
  (`AZ_JUDGE_LLM_MODEL=""` + re-apply), which restores scores without a
  redeploy.
- **Cause confirmed 2026-08-31** (#314): this `azure_ai/mistral-medium-3-5`
  deployment's serving backend has grammar-constrained decoding disabled
  (`--grammar-backend none`), so it rejects *any* `response_format` a
  structured judge call could send — `json_schema` and `json_object` alike,
  verified directly against the endpoint. Not a schema-keyword problem, and
  not something this repo's Terraform can reconfigure (a Foundry serverless
  deployment's launch parameters aren't exposed to the customer). Rollback
  condition 4 also fired the same day, for the same reason, on
  `/v1/assign-codes` — its judge has no degradation path, so it 502s outright
  rather than degrading to `quality_score=null`.
- **All five judge call sites converted to free-text scoring, 2026-08-31**
  (#314): `CodingService`'s per-level judge, the `analyze` judge, and the
  hierarchical leaf judge now parse a `SCORE:`/`EXPLANATION:` or
  `QUALITY_SCORE:`/`UNCERTAINTY_EXPLANATION:` reply instead of requesting a
  `response_format`, matching the pattern `summarize.py`'s two judge sites
  already used successfully against this deployment. No judge call site
  requests structured output any more — only the *generation* calls on the
  primary connection (`CodingResponse`, `SummaryResultModel`,
  `AggregateSummaryResultModel`) still do, and those are unaffected since
  they run on a different model. This closes the incompatibility this ADR's
  rollback conditions 3 and 4 both trace back to.

## Follow-up

#299 previously proposed converting the two free-text `summarize`/
`summarize_bulk` judge sites to structured output, to match what the
`analyze` and coding judges did at the time. That direction is now known to
be wrong: #314 showed the opposite conversion (structured → free text) is
what actually works against this deployment, and every judge site now
follows `summarize`'s original free-text pattern instead. #299 has been
closed as obsolete — there is no longer an asymmetry between judge sites to
fix.

## Participants

Olaf
