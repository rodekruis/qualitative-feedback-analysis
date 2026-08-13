Part of the `Orchestrator` decomposition epic (#112).

Depends-on: #262
Depends-on: #256

## Spec

**What:** Extract `Orchestrator.assign_codes` (~line 1290) and its four private helpers
into `CodingService` in `src/qfa/services/coding.py`, add a `get_coding_service` DI
provider, and repoint the assign-codes route (`src/qfa/api/routes.py`, ~line 442) at it.

Members that move:

| Member | Current line |
|---|---|
| `assign_codes` | ~1290 |
| `_check_coding_deadline` | ~1457 |
| `_traverse_coding_level` | ~1464 |
| `_pick_code_indices` | ~1534 |
| `_judge_code_level` | ~1568 |

Plus the module-level coding helpers `_ScoredCode` (~248) and
`_combine_rejected_explanations` (~265).

**Why:** `assign_codes` is the extraction the 2026-05-08 architecture review called out by
name — *"160 lines of nested for-loops over hierarchy levels — a clear candidate to
extract into `CodingService` with its own `_pick_indices` and `_judge_level` helpers as
private methods."* It is the most self-contained large use case: its four helpers are used
by nothing else, and its hierarchy traversal is unrelated to any other method on the
class.

**`Depends-on: #256` is a conflict-avoidance dependency, not a functional one.** PR #260
(which closes #256) rewrites `_combine_rejected_explanations`, adds `_as_whole_percentage`
and `_ScoredCode.decisive_explanation`, and publishes new module-level constants
(`NO_CODING_LEAD`, `NO_CODING_EMPTY_CONTENT_EXPLANATION`,
`NO_CODING_NOTHING_RELEVANT_EXPLANATION`) from `qfa.services.orchestrator` that
`qfa.api.routes` imports. Extracting the coding path before that lands would produce a
large, avoidable merge conflict across both files. Waiting until #256 closes means this
extraction moves the *final* shape of those helpers.

## Acceptance criteria

- [ ] `src/qfa/services/coding.py` defines `class CodingService:` with **no base class**,
      taking an `LLMCallExecutor` as a constructor dependency.
- [ ] `assign_codes` and the four private helpers listed above are moved onto it and
      **deleted** from `Orchestrator`.
- [ ] `_ScoredCode`, `_combine_rejected_explanations`, `_as_whole_percentage`, and the
      `NO_CODING_*` constants introduced by #260 move to the coding module.
- [ ] `qfa.api.routes` imports the `NO_CODING_*` constants from their new home; the
      empty-content short-circuit in the assign-codes route still returns the same
      explained entry, and the `import-linter` "Enforce hexagonal layers" contract still
      holds (`qfa.services` must not import `qfa.api`).
- [ ] `get_coding_service` is added to `src/qfa/api/dependencies.py`.
- [ ] The assign-codes route handler type-annotates against `CodingService` and no longer
      takes `Orchestrator`.
- [ ] `src/qfa/api/composition.py` constructs the service, sharing the one
      `LLMCallExecutor` instance.
- [ ] Tests move to `tests/services/test_coding.py`, constructing the real
      `LLMCallExecutor` over `FakeLLMPort` / `FakeAnonymizer`. The confidence-threshold
      tests and the six formatter tests added by #260 — including
      `test_message_matches_the_documented_layout`, which pins an exact rendered string —
      are carried over intact and still assert the same exact output.
- [ ] The existing e2e/API tests for the assign-codes endpoint pass **unmodified**.
- [ ] `make test` and `make lint` pass, including all three `import-linter` contracts.
