# ADR-022: `EvaluationPort` for live judge scores in Langfuse

## Status

Accepted

## Context

#354 asks for judge scores in Langfuse, joined to the request that produced
them. #354 is blocked by #353, which asked for exactly this decision as a
written ADR. #353 closed as complete on 2026-09-17, but it left no ADR and
no linked pull request. This ADR answers #353's questions, scoped to what
#354 needs. #353 also asked where `eval/` sits in the architecture, and
where the analyze corpus's ground truth lives. #354 does not touch either
question, so this ADR leaves both open.
[ADR-023](023-eval-black-box-and-langfuse-ground-truth.md) answers them.

#351 and #352 already give every judge call a `JudgeComponents` value:
`faithfulness`, `coverage`, `clarity`, and a derived `quality_score`. Both
are merged, as PR #374 and PR #377.

A live call-tracing integration already existed before this ADR.
`qfa.api.composition.build_langfuse_tracer` wraps every
`LiteLLMClient.complete` call in one OpenTelemetry span, sent to Langfuse
over OTLP/HTTP. Reading that code turned up a fact #354's own task list did
not account for: each span opens its own, unrelated trace id today. A
request with a generation call and a judge call therefore produces two
disconnected Langfuse traces, not one. #354's task 5 asks that a live
score's trace id be the request's `call_id`. No span carries `call_id` as
its trace id yet. A score sent that way sits in Langfuse with no matching
trace to join.

## Decision

1. **A new port, not the executor.** `EvaluationPort`, declared in
   `qfa.domain.ports`, carries one method:
   `record_score(trace_id, name, value)`. `LLMCallExecutor` stays
   composition-only scaffolding per ADR-017 and is not a driven port.
   Evaluation delivery is a real external-system boundary, the same kind
   of boundary `LLMPort` and `AnonymizationPort` already cross, so it
   belongs beside them.

2. **Two adapters, a real null object.**
   `qfa.adapters.evaluation.LangfuseEvaluationAdapter` sends scores
   through the `langfuse` Python client. `NoOpEvaluationAdapter` discards
   them. The composition root always builds one or the other, so no call
   site checks whether Langfuse is configured.

3. **`langfuse` becomes a production dependency.** It moves out of the
   `dev` dependency group. The existing call tracer deliberately avoided
   the `langfuse` package, using raw OTLP export instead, to keep it
   dev-only. Score creation has no such raw-OTLP equivalent in this
   codebase, so this ADR accepts the move #354 already asked for.

4. **`call_id` becomes the real Langfuse trace id.**
   `qfa.services.call_context.otel_context_for` builds an OpenTelemetry
   context that carries a placeholder span. That span's trace id is
   `call_id.int`. `LiteLLMClient.complete`, the one real span-creation
   call site, passes this context explicitly as `context=`. `call_scope`
   itself does not attach this context to the ambient OTel context. The
   ambient context is a single, provider-agnostic global. Application
   Insights' auto-instrumentation shares it. An attach there reparents
   every unrelated span opened during the request, for example DB
   statements and outbound HTTP calls, under `call_id` too. A request's
   generation and judge calls therefore land in one Langfuse trace with
   several observations, not in several separate traces, and no other
   trace changes. This also changes the shape of the existing
   call-tracing feature, not only this ticket's addition.

5. **Failure is handled by the client, not by this adapter.** The
   installed `langfuse` client's `create_score` does no synchronous I/O.
   It queues the score for a background thread to send, and it catches
   every exception itself rather than raising one. A Langfuse outage
   therefore never reaches the caller. This adapter adds no timeout and no
   retry logic on top of that guarantee.

6. **Hierarchical analysis sends one set of four scores per chunk**, from
   each leaf judge, not one aggregate for the whole request. A
   request-level average hides which chunk the judge actually scored.

7. **The scores run beside the existing log line, not instead of it.**
   `qfa.services.judge_scoring.log_judge_components` (#351) still writes
   one `judge components:` line per judged call. The line stays for
   grep-based debugging. Langfuse becomes the durable, queryable copy.

8. **No sampling.** Every judged request, and every judged chunk in a
   hierarchical run, sends its scores at full rate.

## Options considered

### Trace identity: score the judge's own trace instead (rejected)

Leave the call tracer as it is, one trace per LLM call. Send a score
against the judge span's own trace id, rather than `call_id`. Rejected:
this drops the shared key between Postgres (`LLMCallRecord.call_id`) and
Langfuse that #354's task 5 asks for. It also still shows a generation call
and the judge call that graded it as two unrelated traces in the Langfuse
UI. That is the opposite of what "join a quality score to the run that
produced it" means in practice.

### Trace identity: send the score anyway, accept an orphaned trace (rejected)

Keep the tracer untouched and send the score against `trace_id=call_id.hex`
regardless. Rejected: the score joins `LLMCallRecord` in Postgres but shows
disconnected from any observation in the Langfuse UI. That defeats the
ticket's own stated goal: a production request comparable to an offline
benchmark trace.

### Port design: hang scoring off `LLMCallExecutor` (rejected)

ADR-017 already settled what shape `LLMCallExecutor` takes: shared call
scaffolding, never a driven port. Evaluation delivery talks to an external
system. That is exactly what a port is for in this codebase, so it does
not belong on the executor.

### Null object vs `EvaluationPort | None` (rejected the latter, at the composition root)

`EmbeddingPort` uses `Port | None` at the service boundary, with one
`is None` check at its single call site. `EvaluationPort` is called from
four call sites across two services. A real no-op adapter, decided once in
`qfa.api.composition.build_evaluator`, answers "is Langfuse configured"
exactly once, at the composition root. That matches #354's own request for
a no-op adapter. Each service constructor still defaults its own `evaluator`
parameter to `None`, for tests and scripts that do not care about scoring.
`record_judge_scores` treats that `None` as a no-op, so nothing outside
composition-root wiring depends on the null object existing.

## Consequences

- `qfa.domain.ports.EvaluationPort`,
  `qfa.adapters.evaluation.LangfuseEvaluationAdapter`,
  `qfa.adapters.evaluation.NoOpEvaluationAdapter`, and
  `qfa.api.composition.build_evaluator` are new.
- `AnalyzeService` and `SummarizeService` each take an `evaluator`
  constructor argument, defaulted to `None`.
- `qfa.services.judge_scoring.record_judge_scores` is the one place a
  `JudgeComponents` value becomes four Langfuse scores. Every call site
  (`analyze_bulk`, the hierarchical leaf judge, `summarize`,
  `summarize_bulk`) calls it, never the port directly.
- The import-linter "Enforce hexagonal layers" contract needed one new
  `ignore_imports` line, `qfa.api.composition -> qfa.adapters.evaluation`,
  matching the existing embedder and anonymiser entries.
- Every Langfuse trace, judged or not, now carries `deployed_version` and
  `deployed_commit` as trace metadata. These are the same field names
  `eval/assign_codes_eval.py` already uses for its own Dataset Run
  metadata.
- `pyproject.toml` moves `langfuse>=4.6` from the `dev` dependency group to
  `dependencies`.

## When to revisit

- If production write volume makes Langfuse ingestion cost or throughput a
  real problem, revisit the no-sampling decision in point 8.
- If hierarchical's per-chunk scores prove too chatty to read, revisit
  point 6. A request-level aggregate is one option, or a
  chunk-count-weighted mean matching how `confidence` is already
  aggregated.
- If a future adapter needs real I/O, for a provider whose scoring call is
  not fire-and-forget, `EvaluationPort.record_score` can gain an async
  variant then. `EmbeddingPort.embed`'s docstring already names this same
  escape hatch.

## Participants

Olaf
