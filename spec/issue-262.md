Part of the `Orchestrator` decomposition epic (#112). This is the shared foundation every
service extraction builds on, so it lands second (right after the ADR) and changes no
public behaviour.

Depends-on: #261

## Spec

**What:** Extract the LLM-call scaffolding currently spread across `Orchestrator`'s
private methods into a new plain class `LLMCallExecutor` in
`src/qfa/services/llm_call_executor.py`. `Orchestrator` keeps all six public methods with
identical signatures and delegates the extracted work to an injected executor.

Methods that move:

| Method | Current line | Role |
|---|---|---|
| `_anonymize_records` | ~839 | PII redaction before the LLM call |
| `_bounded_complete` | ~860 | the retrying, deadline-bounded LLM call |
| `_check_deadline_and_get_timeout` | ~1435 | per-call timeout derivation |
| `_check_token_limit` | ~1600 | pre-flight token budget guard |

**Why:** Every use-case service needs this same scaffolding. Deciding *how* it is shared
is the whole architectural question this epic answers, and #112 settled it: a single
injected collaborator, not a base class. Landing the collaborator on its own — while
`Orchestrator` is still intact and its full test suite still green — proves the seam is
correct before four extractions commit to it. If the executor's boundary is wrong, this
is the cheapest possible place to discover it.

No route, DI, or response change happens here.

## Acceptance criteria

- [ ] `src/qfa/services/llm_call_executor.py` defines `class LLMCallExecutor:` with
      **no base class** and no `Protocol`.
- [ ] `LLMCallExecutor` is **not** referenced from `qfa/domain/ports.py`.
- [ ] Its constructor takes the driven ports and config it needs — `llm: LLMPort`,
      `anonymizer: AnonymizationPort`, `settings: OrchestratorSettings`,
      `llm_timeout_seconds: float`, `max_total_tokens: int`.
- [ ] The four methods above are moved onto it (renamed to public names where they are
      now cross-class calls) and **deleted** from `Orchestrator`.
- [ ] `Orchestrator` holds an `LLMCallExecutor` and delegates; all six public methods
      (`analyze_bulk`, `analyze_hierarchical`, `summarize_bulk`, `summarize`,
      `assign_codes`, `detect_sensitive_content`) keep byte-identical signatures.
- [ ] `build_orchestrator` in `src/qfa/api/composition.py` constructs the executor and
      passes it in; `build_orchestrator`'s own signature is unchanged.
- [ ] New `tests/services/test_llm_call_executor.py` covers the executor directly:
      retry-on-transient, non-transient passthrough, deadline expiry, token-limit
      rejection, and anonymize→deanonymize round-tripping — constructed over the existing
      `FakeLLMPort` / `FakeAnonymizer`, not over a new fake executor.
- [ ] `tests/services/test_orchestrator.py` and
      `tests/services/test_orchestrator_hierarchical.py` still pass with changes limited
      to how the `Orchestrator` under test is constructed. No test assertion is weakened
      or deleted to accommodate the refactor.
- [ ] `make test` and `make lint` pass, including all three `import-linter` contracts.
- [ ] Behaviour-preserving: no HTTP request/response shape, status code, or error payload
      changes.
