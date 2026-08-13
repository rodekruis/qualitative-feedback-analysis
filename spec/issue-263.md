Part of the `Orchestrator` decomposition epic (#112). This is the smallest extraction and
lands first among the four, as the end-to-end proof of the pattern.

Depends-on: #262

## Spec

**What:** Extract `Orchestrator.detect_sensitive_content` (~50 lines, currently around
line 1385) into `SensitivityService` in `src/qfa/services/sensitivity.py`, add a
`get_sensitivity_service` DI provider, and repoint the sensitivity route
(`src/qfa/api/routes.py`, ~line 512) at it.

**Why:** `detect_sensitive_content` is the smallest and most self-contained of the six use
cases — one LLM call, no helper methods, no embedder. That makes it the cheapest possible
first full traversal of the pattern: new service class → DI provider → route rewire →
tests moved → composition updated. Any friction in the approach (import-linter
complaints, DI wiring awkwardness, test-fixture churn) surfaces here on a 50-line change
rather than on the 700-line `AnalyzeService`. The three larger extractions then copy a
proven shape.

## Acceptance criteria

- [ ] `src/qfa/services/sensitivity.py` defines `class SensitivityService:` with **no
      base class**, taking an `LLMCallExecutor` as a constructor dependency.
- [ ] `detect_sensitive_content` is moved onto it and **deleted** from `Orchestrator`.
- [ ] `get_sensitivity_service` is added to `src/qfa/api/dependencies.py`.
- [ ] The sensitivity route handler type-annotates against `SensitivityService` and no
      longer takes `Orchestrator`.
- [ ] `src/qfa/api/composition.py` constructs the service (sharing the one
      `LLMCallExecutor` instance with the still-present `Orchestrator`).
- [ ] Tests for this use case move to `tests/services/test_sensitivity.py`, constructing
      the real `LLMCallExecutor` over `FakeLLMPort` / `FakeAnonymizer`. No test assertion
      is weakened or deleted in the move.
- [ ] The existing e2e/API tests for the sensitivity endpoint pass **unmodified** —
      response shape, status codes, and error payloads are unchanged.
- [ ] `make test` and `make lint` pass, including all three `import-linter` contracts.
