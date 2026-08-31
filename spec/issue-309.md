## Summary

`/v1/analyze-bulk`'s judge call (`AnalyzeService`, `src/qfa/services/analyze.py`)
fails with a `400 BadRequestError` against `azure_ai/mistral-medium-3-5`
whenever `JUDGE_LLM_MODEL` routes judge calls to it. The request itself still
returns `200` because the judge call is wrapped in try/except and degrades to
`quality_score=null` + the unavailable-judge explanation
(`analyze.py:255-283`) — but that means **every analyze-bulk confidence score
is currently silently missing** while the judge connection is active.

## Evidence

dev-test, 2026-08-31:

```
2026-08-31 08:41:32,875:DEBUG:qfa.adapters.llm_client:LiteLLMClient: dispatching message with per-attempt timeout 230.0s (retry budget 690.0s)
2026-08-31 08:41:35,532:DEBUG:qfa.adapters.llm_client:LLM call: model=gpt-5.4-2026-03-05 latency=2.66s prompt_tokens=414 completion_tokens=76 cost=0.002175
2026-08-31 08:41:35,552:DEBUG:qfa.adapters.llm_client:LiteLLMClient: dispatching message with per-attempt timeout 230.0s (retry budget 690.0s)
LiteLLM.Info: If you need to debug this error, use `litellm._turn_on_debug()'.
2026-08-31 08:41:36,385:ERROR:qfa.adapters.llm_client:LLM provider error: type=BadRequestError status=400 model=azure_ai/mistral-medium-3-5
2026-08-31 08:41:36,407:WARNING:qfa.services.analyze:Analyse judge call failed: error_class=LLMBadRequestError
2026-08-31 08:41:36,559:INFO:qfa.api.app:POST /v1/analyze-bulk status=200 duration=3812ms request_id=a3d85bce-9057-489a-b1f4-22206660bb6a tenant=dev-test-user-0
```

The first call (`gpt-5.4`, the primary generation call) succeeds; the second
call (the judge, routed to `azure_ai/mistral-medium-3-5`) 400s. The LiteLLM
debug output that would show the actual error response body prints via
LiteLLM's own logger, not `qfa.adapters.llm_client`, so **the precise 400
reason is not captured in application logs** — this is the main blocker to
a fix.

## Suspected cause

`AnalyzeJudgeResult` (`analyze.py:66-77`) is passed as `response_model`,
which builds a structured `response_format` via `_provider_safe_response_format`
(`llm_client.py:82-90`). That helper already strips JSON-Schema keywords
(`minimum`, `maxLength`, etc.) known to make Azure AI Mistral reject a schema
— added for a prior, similar incompatibility (see
`_UNSUPPORTED_SCHEMA_KEYWORDS`, `llm_client.py:36-61`, and its test coverage
in `tests/adapters/test_llm_client.py`) — but `AnalyzeJudgeResult`'s schema
may still carry something else Mistral rejects (e.g. `title`, `$defs`,
`additionalProperties`, or the object shape itself). **Unconfirmed** without
the actual response body.

## What's needed to fix

1. Capture the full LiteLLM debug output / raw response body for one 400
   (`litellm._turn_on_debug()`, or otherwise surface LiteLLM's own log
   stream) to pin the exact rejected schema element.
2. Extend `_UNSUPPORTED_SCHEMA_KEYWORDS` / `_provider_safe_response_format`
   (or otherwise adjust what `AnalyzeJudgeResult` emits) to match what
   Mistral's structured-output endpoint accepts.
3. Add regression coverage in `tests/adapters/test_llm_client.py` (or
   wherever the existing schema-stripping tests live) asserting the analyze
   judge's built `response_format` is well-formed against Mistral's known
   constraints.

## Priority

P1 (human) — confidence scores are silently broken on every analyze-bulk
call while the judge connection is active.

## Explicitly out of scope for this issue

- **#299** — the two free-text `summarize`/`summarize_bulk` judge sites have
  no structured response contract and no error handling at all around the
  judge call, so a judge failure there can crash the whole request instead
  of degrading (unlike this issue's site, which already degrades gracefully).
  ADR-020 (`docs/adr/020-mistral-medium-as-judge-model.md`) names this
  failure mode as one of its own rollback triggers. Already tracked there,
  not duplicated here. (#298 was an exact duplicate of #299 and has been
  closed.)
- **#310** — extending the judge/primary split to `CodingService.assign_codes`,
  which #258 deliberately left out. Different service, different change;
  split out so each can be reviewed on its own.
