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
every judge call site in every deployed environment: the `analyze` judge,
the hierarchical leaf judge, the judges in `summarize` and `summarize_bulk`,
the per-level judge in `assign_codes` (added by #310 after this ADR was
accepted — #258's exclusion of the coding path was scope-only, no
coding-specific concern was ever recorded), and the classification judge in
`detect_sensitive_content` (added later still, on the same terms). Configured via `JUDGE_LLM_MODEL`
+ `JUDGE_LLM_API_BASE` (`var.judge_llm_model`, `var.judge_llm_api_base` in
`infra/variables.tf`), no
new credential — the judge connection inherits `LLM_API_KEY` from the primary
connection, same Azure Foundry resource, same trust boundary.

The judge model stays outside the model family of the primary model (GPT), so
that no model grades its own answers. A replacement judge must follow this rule.

## How it was verified

Not before the rollout: no evaluation harness existed then. The staged rollout
and the rollback conditions below limited the risk. #436 later measured the
analyze judge with the analyze eval (see Evidence).

## Evidence

#436 (2026-10-16), analyze judge only, case set `analyze/cases-frequent-en-v1`
(10 cases):

- Before: in the dev CI run `cases-frequent-en-v1-full-20261014-192011`, the
  judge gave faithfulness, coverage and clarity 1.0 on all 10 cases, with almost
  the same explanation. P01 found 7 of 10 expected items and still got coverage
  1.0.
- After the stricter prompt (`analyze-judge` version 2), in two local runs with
  the dev judge
  ([run 1](https://langfuse.510.global/project/cmtlny8oi0001t807r9xv8exn/datasets/cmulbr6uo000jt807svhm3cts/runs/0986ac0edffaf536),
  [run 2](https://langfuse.510.global/project/cmtlny8oi0001t807r9xv8exn/datasets/cmulbr6uo000jt807svhm3cts/runs/69f984ab8c2be67c)):
  faithfulness 0.75–1.00, coverage 0.70–1.00, clarity 0.70–0.90. Each
  explanation names specific problems.
- Coverage does not follow `item_recall` reliably. P01 got coverage 0.80 in
  run 1 and 1.00 in run 2. One case can move by 0.2 between runs.
- The judge call takes 2.8–4.3 s on average instead of 1.3 s, because it
  writes the problem lists before the scores.

## Limitations

- The analyze judge gives graded scores since #436, but its coverage does not
  follow the reference scores reliably. The other judge call sites have no
  discrimination evidence yet.
- Every judge call site parses free text via regex (`parse_judge_components`
  for `analyze`/`summarize`, `parse_judge_response` for the
  `SCORE:`/`EXPLANATION:` replies that `assign_codes` and
  `detect_sensitive_content` share) and treats anything unparseable or out of
  range as a failure — a weaker or differently-tuned model is more likely to
  break that contract than a schema-enforced response would be. See the
  follow-up issue below.
- The `assign_codes` per-level judge has **no degradation path**: an
  out-of-range score raises `AnalysisError` and an unparseable structured
  response raises `LLMResponseParseError`, either of which fails the
  `/v1/assign-codes` request — unlike the two `analyze` judges, which fall
  back to `quality_score=None`, and unlike the `detect_sensitive_content`
  judge, which falls back to `confidence=None`. Same theme as #299.
- That call site issues `complete()` without a `timeout`, so it runs on
  `LiteLLMClient`'s default per-attempt budget rather than a deadline-derived
  one. Pre-existing, but the budget now applies to a different model.

## Rollout

dev → staging → prd, one environment at a time via the per-environment
`AZ_JUDGE_LLM_MODEL` and `AZ_JUDGE_LLM_API_BASE` GitHub Actions variables and a
`terraform apply`, each soaking at least one working day before promoting to the
next environment. Until #436, the apply step did not pass `AZ_JUDGE_LLM_MODEL`,
so apply always used the default in `infra/variables.tf`. That default is this
model, so the rollout still worked.

## Rollback

Set the environment's `AZ_JUDGE_LLM_MODEL` (and `AZ_JUDGE_LLM_API_BASE`, if it
changes) back to the previous judge model. Run Terraform `plan`, make sure that
only the judge settings change, then `apply`. This is a config change with **no
code change and no redeploy of the image**.

An empty or unset `AZ_JUDGE_LLM_MODEL` removes `JUDGE_LLM_MODEL`, so judge calls
fall back to the primary client, `azure/gpt-5.4` (`src/qfa/settings.py`). That
breaks the family rule in Decision. Use it only when the judge endpoint is down.
Judge scores from that time do not compare with other runs.

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
- **Analyze judge returns three components, 2026-09-18** (#351): the analyze
  judge prompt now asks for four lines —
  `FAITHFULNESS:` / `COVERAGE:` / `CLARITY:` / `UNCERTAINTY_EXPLANATION:`
  (explanation last, so the greedy explanation group cannot swallow a
  component). Python parses the three floats and computes `quality_score`
  from `QUALITY_SCORE_WEIGHTS`. The reply is still free text, still
  regex-parsed — this widens the same decision as #314 rather than reversing
  it. Hierarchical leaf judges share the parser; the hierarchical response
  still returns the three components as `null`.
- **Summarize judges return three components too, 2026-09-18** (#352): both
  `summarize_bulk` and `summarize` now ask their judge for the same
  `FAITHFULNESS:` / `COVERAGE:` / `CLARITY:` / `UNCERTAINTY_EXPLANATION:`
  reply and reuse `parse_judge_components`/`QUALITY_SCORE_WEIGHTS` from #351
  — no bare float and no second weights copy. The explanation line is parsed
  but discarded: unlike `AnalysisResultModel`, the summarize result models
  carry no `uncertainty_explanation` field. Out-of-range or unparseable
  replies still raise `AnalysisError` → 502, unchanged from before this
  ticket; the two summarise judges have no `null`-degradation path (the risk
  the second Limitations bullet above describes).
- **Analyze judge lists problems before it scores, 2026-10-16** (#436): the
  judge gave 1.0 to every analyze case (see Evidence). The prompt now asks for
  `UNSUPPORTED_CLAIMS:`, `MISSED_THEMES:` and `CLARITY_PROBLEMS:` first, and
  scores each criterion from its list, with anchors at 1.0, 0.9, 0.75, 0.5,
  0.25 and 0.0. The reply still ends with the four lines, so the parser did not
  change. The model stays. #449 tries Mistral Large 4 when it is available in
  our Azure AI Foundry resources.

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
