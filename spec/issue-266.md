Part of the `Orchestrator` decomposition epic (#112). This is the largest extraction
(~700 lines) and lands last among the four, on a pattern already proven by the other
three.

Depends-on: #262

## Spec

**What:** Extract both analyze modes and their helpers into a single `AnalyzeService` in
`src/qfa/services/analyze.py`, add a `get_analyze_service` DI provider, and repoint the
analyze route (`src/qfa/api/routes.py`, ~lines 225–227, which selects between the two
modes) at it.

Members that move:

| Member | Current line | Mode |
|---|---|---|
| `analyze_bulk` | ~369 | bulk |
| `analyze_hierarchical` | ~491 | hierarchical |
| `_map_chunk` | ~906 | hierarchical |
| `_judge_chunk` | ~934 | hierarchical |
| `_reduce_partials` | ~988 | hierarchical |
| `_group_partials_to_budget` | ~1088 | hierarchical |
| `_coverage_weighted_mean` | ~1128 | hierarchical |
| `_is_retained_analyze_placeholder` | ~357 | both |
| `_ANALYZE_RETAINED_PLACEHOLDER_TYPES` | ~331 | both |

Plus the module-level analyze/judge helpers `AnalyzeJudgeResult` (~175),
`_parse_judge_quality_score` (~189), `_build_judge_system_message` (~201), and
`_SlotTiming` (~278), to the extent they are used only by the analyze path.

**Why:** `analyze_bulk` and `analyze_hierarchical` are two modes of one endpoint — the
route already selects between them on the request — and they share the
retained-placeholder rule (`_ANALYZE_RETAINED_PLACEHOLDER_TYPES`), which is a
defense-in-depth guardrail that must stay in one place. Splitting them into two services
would either duplicate that rule or require a third object to hold it, for no gain. They
therefore stay in one `AnalyzeService`, per the decision taken in #112.

`AnalyzeService` is the only use case that needs the `EmbeddingPort` — the hierarchical
path requires it, and `None` makes that path raise `AnalysisError` at request time. That
dependency becomes explicit on this one service's constructor instead of sitting on a
shared constructor that four other use cases do not need. This is precisely the payoff
the composition-only decision was made for.

## Acceptance criteria

- [ ] `src/qfa/services/analyze.py` defines `class AnalyzeService:` with **no base
      class**, taking an `LLMCallExecutor` and an `embedder: EmbeddingPort | None` as
      constructor dependencies.
- [ ] All members in the table above are moved onto it and **deleted** from
      `Orchestrator`; module-level helpers used only by the analyze path move with it.
- [ ] `_ANALYZE_RETAINED_PLACEHOLDER_TYPES` remains a single definition shared by both
      modes — it is not duplicated.
- [ ] `get_analyze_service` is added to `src/qfa/api/dependencies.py`.
- [ ] The analyze route handler type-annotates against `AnalyzeService`, no longer takes
      `Orchestrator`, and still selects bulk vs. hierarchical on the same request field
      as today.
- [ ] `src/qfa/api/composition.py` constructs the service, sharing the one
      `LLMCallExecutor` instance and passing the embedder built by `build_embedder`.
- [ ] With `embedder=None`, the hierarchical path still raises `AnalysisError` at request
      time with the same message and the same HTTP status as today.
- [ ] Tests move to `tests/services/test_analyze.py` and
      `tests/services/test_analyze_hierarchical.py`, constructing the real
      `LLMCallExecutor` over `FakeLLMPort` / `FakeAnonymizer`. No test assertion is
      weakened or deleted — the anonymization-ordering, prompt-injection, output-language,
      token-limit, judge-failure, and concurrency cases are all carried over intact.
- [ ] The existing e2e/API tests for the analyze endpoint pass **unmodified** in both
      modes.
- [ ] `make test` and `make lint` pass, including all three `import-linter` contracts.
