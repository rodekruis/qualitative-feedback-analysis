# ADR-017: Decompose the Orchestrator by composition only

## Status

Accepted (supersedes decision 3 of
[ADR-011](011-drop-orchestrator-port.md); its decisions 1 and 2 stand)

## Context

`qfa.services.orchestrator.Orchestrator` is a god-class. It has grown from
723 lines at the 2026-05-08 architecture review (`docs/architecture-review-2026-05-08.md`,
kept out of the published site) to over 1700, absorbing an entire
hierarchical map/judge/reduce analysis mode
as further methods on the same class. Its six public methods —
`analyze_bulk`, `analyze_hierarchical`, `summarize_bulk`, `summarize`,
`assign_codes`, `detect_sensitive_content` — share a constructor but little
else, so every reader must traverse the whole file to change any one of them
and no use case can be tested or evolved in isolation.

Epic #112 decomposes it into per-use-case application services
(`AnalyzeService`, `SummarizeService`, `CodingService`,
`SensitivityService`). This is the escape valve ADR-011 explicitly
anticipated — *"extracting one into its own class when it grows enough to
earn its own collaborators"* — and the change the 2026-05-08 review
recommended; its addendum is explicit that the extraction is *"anticipated
by ADR-011, not contrary to it"*.

The class ADR-011 calls `StandardOrchestrator` was later renamed
`Orchestrator`. It is one and the same class; this ADR uses the current
name throughout.

Splitting one class into five raises a question the epic cannot leave
open: **how do the new services share the behaviour they genuinely have in
common?** Four private helpers are used by more than one use case —
`_anonymize_records`, `_bounded_complete`, `_check_deadline_and_get_timeout`
and `_check_token_limit` — together with the constructor state they close
over (the LLM port, the anonymizer, orchestrator settings, the per-call
timeout and the total-token ceiling).

Six implementation PRs follow this one (#262–#267), written at different
times by different agents and developers. Without a written record, each
one re-litigates "should these services share a base class?" and the
answers drift across the epic. This ADR is the contract those PRs are
reviewed against, which is why it lands first and on its own.

## Decision

1. **Composition only — no shared base class among the new services.**
   The shared LLM-call scaffolding (retry, deadline enforcement,
   token-limit check, anonymize/deanonymize) is extracted into a single
   collaborator object, `LLMCallExecutor`, which each service receives as
   a constructor dependency and delegates to. No use-case service
   inherits from another class in `qfa.services`.

2. **`Orchestrator` is deleted, not kept as a facade.** Each route handler
   gets its own dependency provider (`get_analyze_service`,
   `get_coding_service`, …) and type-annotates against the single
   use-case service it actually uses.

3. **`LLMCallExecutor` is a plain concrete class, not a Protocol.** It is
   a services-layer collaborator, not a driven port — it wraps no
   infrastructure — so it is not declared in `qfa.domain.ports` and has no
   Protocol base. Service tests construct the **real** executor over the
   existing `FakeLLMPort` / `FakeAnonymizer` test doubles, which is the
   pattern ADR-011 already calls preferable to stubbing the application
   service itself.

4. **The resulting codebase-wide rule: inheritance is used *only* for
   declarative port↔adapter conformance.** Per
   [ADR-002](002-protocol-based-ports.md) and `AGENTS.md`, a class
   inherits from another class in this codebase for exactly one reason —
   to declare that it implements a port
   ({py:class}`~qfa.domain.ports.LLMPort`,
   {py:class}`~qfa.domain.ports.AnonymizationPort`,
   {py:class}`~qfa.domain.ports.EmbeddingPort`), inheriting zero
   behaviour. **Behaviour reuse is always composition.** This refactor
   adds no new inheritance.

The net answer to "inheritance, composition, or hybrid?" is therefore a
*disciplined hybrid*: inheritance is a declaration of conformance and
nothing else; every shared implementation is an injected collaborator.

## Options Considered

### A. Per-use-case services + injected `LLMCallExecutor` (chosen)

- Each service's constructor names exactly the dependencies it uses, so a
  reader can see the real dependency surface of one use case without
  reading the others. `AnalyzeService` takes an embedder;
  `SensitivityService` does not, and says so by omission.
- The shared scaffolding lives in one place with one set of tests, and is
  reachable from a service only through a named attribute — grep for
  `LLMCallExecutor` and you have every user of it.
- Keeps inheritance meaning exactly one thing (see decision 4), so
  `class X(Y):` anywhere in the codebase remains readable as "X implements
  port Y".
- **Con:** one more object to wire in `qfa.api.composition`, and one more
  hop between a use case and the LLM call it makes. Judged a fair price
  for the executor being independently testable and explicitly injected.

### B. Shared abstract base service class (rejected)

A `BaseService` holding the four helpers, with each use-case service
inheriting from it.

- **Con:** it makes each service's real dependencies implicit. The
  constructor moves to the base, so the base must accept the union of
  everything any subclass needs (including the embedder that only the
  analyze path uses) and every service silently advertises dependencies it
  never touches.
- **Con:** it reintroduces the inheritance-for-reuse style this codebase
  has consistently avoided, and destroys the signal established by
  ADR-002: a reader seeing `class CodingService(BaseService):` can no
  longer read inheritance as "this implements a port".
- **Con:** shared base classes are magnets. The next helper that two
  services need lands on the base rather than in a collaborator, and the
  god-class reappears one rung up the hierarchy — which is the exact
  failure mode this epic exists to undo.

### C. Keep `Orchestrator` as a delegating facade (rejected)

Retain the class with its six public methods, each forwarding to the
matching service.

- **Pro:** route handlers, composition root and existing tests need no
  changes; the epic could stop after the extractions.
- **Con:** rejected on ADR-011's own reasoning. That ADR removed
  `OrchestratorPort` because "a layer of indirection that readers must
  traverse for no payoff" is dead weight — and a pure-forwarding facade is
  precisely that same dead weight, now as a concrete class rather than a
  protocol.
- **Con:** it preserves the coupling the epic removes: every route still
  depends on a type that transitively reaches every use case, so
  "which endpoints does this change affect?" stays unanswerable from the
  type signature.

### D. Keep `Orchestrator` as an attribute container (rejected)

Retain the class as a namespace holding the services
(`orchestrator.analyze.analyze_bulk(...)`) rather than forwarding methods.

- **Pro:** thinner than a facade — no method bodies to keep in sync with
  the services.
- **Con:** the coupling problem of option C is unchanged. A handler
  annotated against the container still depends on all five services; only
  the call syntax got longer.
- **Con:** it forces the dependency graph to build every service (and the
  embedder they do not all need) for every request, whichever endpoint was
  hit.

### E. Declare `LLMCallExecutor` as a Protocol port (rejected)

Put an `LLMCallExecutorPort` Protocol in `qfa.domain.ports` and inject
implementations of it.

- **Con:** it is not a port. Ports in this codebase invert dependencies on
  *infrastructure* — they keep the OpenAI SDK and Presidio's spaCy models
  outside the application ring. The executor wraps no external system; it
  orchestrates calls to a port that already exists.
- **Con:** `qfa.domain.ports` holds **driven** ports only, a property
  ADR-011 deliberately established. An application-layer collaborator
  declared there would undo that.
- **Con:** a Protocol with one permanent implementation is the abstraction
  ADR-011 already rejected. The usual counter-argument — "tests need to
  substitute it" — does not apply: service tests use the *real* executor
  over fake driven adapters (decision 3), so there is nothing to swap.

### F. Module-level free functions instead of a collaborator object (rejected)

Export the four helpers as functions from a `qfa.services` module and call
them directly from each service.

- **Pro:** no object to construct or wire; the plainest possible Python.
- **Con:** the helpers are not pure. They close over five
  constructor-scoped values (LLM port, anonymizer, orchestrator settings,
  per-call timeout, token ceiling) that would have to be threaded through
  every call site as arguments — a long, repeated parameter list at every
  LLM call in the codebase, re-derived rather than configured once.
- **Con:** it moves per-request configuration out of the composition root.
  Binding those five values once, at wiring time, is exactly what the
  object is for; free functions would push that responsibility back into
  each use case.

## Relationship to ADR-011

ADR-011 removed `OrchestratorPort` and told route handlers to depend on the
concrete orchestrator class. This ADR takes the escape valve that ADR-011
wrote down for itself — *"extracting one into its own class when it grows
enough to earn its own collaborators"* — and applies it to all six use
cases at once. It is a continuation of ADR-011's reasoning, not a reversal
of it:

- **ADR-011 decision 1 (drop the swappable-orchestrator requirement) —
  stands.** No alternative orchestrator implementations are being
  introduced. The new classes are separate *use cases*, not
  interchangeable strategies for the same use case.
- **ADR-011 decision 2 (no `OrchestratorPort`) — stands, and is
  reinforced.** No driving port is reintroduced. The route handlers depend
  on concrete services, and option E above extends the same reasoning to
  the new collaborator.
- **ADR-011 decision 3 ("type API dependencies and tests against
  `StandardOrchestrator` directly") — superseded.** The class it names no
  longer exists after epic #112. Its *intent* survives unchanged: depend
  on the concrete application service rather than an abstraction over it.
  Only the target moves — from one class shared by all handlers to the one
  use-case service each handler needs.

ADR-011 remains **Accepted** and stays in place; it is not obsolete.

## Consequences

- Six PRs implement this decision: #262 extracts `LLMCallExecutor`, then
  #263–#266 extract `SensitivityService`, `SummarizeService`,
  `CodingService` and `AnalyzeService`, and #267 deletes `Orchestrator`,
  rewires `qfa.api.composition` and refreshes the prose docs.
- Reviewers of those PRs can reject a shared service base class, a
  surviving facade or a new Protocol by citing this ADR, without
  reopening the argument.
- The extractions are incremental, so `qfa.api.composition` temporarily
  builds both the shrinking `Orchestrator` and the already-extracted
  services. That coexistence window is expected and ends with #267.
- `qfa.api.dependencies` gains one provider per use-case service; each
  route handler annotates against exactly one of them.
- Adding a use case after this epic means adding a service class that
  takes `LLMCallExecutor` plus whatever else it needs, and a provider for
  it — not a method on a shared class and not a subclass.
- Service tests construct the real `LLMCallExecutor` over `FakeLLMPort` /
  `FakeAnonymizer`. Those doubles keep inheriting from their ports as
  ADR-002 requires; `LLMCallExecutor` itself has no base class to inherit
  from and needs no double.
- The import-linter contracts are unaffected: every new class lives in
  `qfa.services` and the layer boundaries do not move.
- Prose that describes the orchestrator as "a single application service
  composed of multiple use cases" (`AGENTS.md`, the architecture pages)
  stays accurate until the class is actually removed, and is updated as
  part of #267 rather than here.

## When to revisit

- If a use-case service ever needs to be *substituted* — not merely
  configured with different driven adapters — revisit ADR-011 decision 2
  and this ADR together, since that is the swappability requirement both
  of them declined.
- If `LLMCallExecutor` accumulates responsibilities that only some
  services use, split it into further collaborators rather than
  introducing an inheritance hierarchy; decision 4 is the invariant to
  preserve.

## Participants

Marius
