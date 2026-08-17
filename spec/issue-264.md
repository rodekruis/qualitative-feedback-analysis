Part of the `Orchestrator` decomposition epic (#112).

Depends-on: #262

## Spec

**What:** Extract `Orchestrator.summarize_bulk` (~line 1140) and `Orchestrator.summarize`
(~line 1218) into `SummarizeService` in `src/qfa/services/summarize.py`, add a
`get_summarize_service` DI provider, and repoint both summarize route handlers
(`src/qfa/api/routes.py`, ~lines 316 and 383) at it.

**Why:** These two methods are the clearest natural pair in the class — both are
single-completion summarization over feedback records, differing only in bulk vs.
single-record shape, and neither is used by any other use case. Grouping them into one
service keeps their shared prompt and hyperlinking conventions co-located while removing
~150 lines from `Orchestrator`.

Note the module-level helper `_hyperlink_form_references` (~line 220) is used by the
summarize path; move it with the service unless another retained use case still needs it,
in which case leave it shared and note where it lands.

## Acceptance criteria

- [ ] `src/qfa/services/summarize.py` defines `class SummarizeService:` with **no base
      class**, taking an `LLMCallExecutor` as a constructor dependency.
- [ ] `summarize_bulk` and `summarize` are moved onto it and **deleted** from
      `Orchestrator`.
- [ ] Any module-level helper used only by these two methods (e.g.
      `_hyperlink_form_references`) moves with them; anything still shared with a
      retained use case stays put, and the PR description says which and why.
- [ ] `get_summarize_service` is added to `src/qfa/api/dependencies.py`.
- [ ] Both summarize route handlers type-annotate against `SummarizeService` and no
      longer take `Orchestrator`.
- [ ] `src/qfa/api/composition.py` constructs the service, sharing the one
      `LLMCallExecutor` instance.
- [ ] Tests for these two use cases move to `tests/services/test_summarize.py`,
      constructing the real `LLMCallExecutor` over `FakeLLMPort` / `FakeAnonymizer`. No
      test assertion is weakened or deleted in the move — in particular the
      output-language, hyperlink, and no-trailing-question cases are carried over intact.
- [ ] The existing e2e/API tests for both summarize endpoints pass **unmodified**.
- [ ] `make test` and `make lint` pass, including all three `import-linter` contracts.
