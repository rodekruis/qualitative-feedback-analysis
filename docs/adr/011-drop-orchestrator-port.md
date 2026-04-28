# ADR-011: Drop Swappable-Orchestrator Requirement and Remove OrchestratorPort

## Status

Accepted (supersedes [ADR-008](obsolete/008-keep-orchestrator-port.md))

## Context

[ADR-008](obsolete/008-keep-orchestrator-port.md) decided to keep
`OrchestratorPort` as a `typing.Protocol` in `domain/ports.py`, on the
basis that the README explicitly promised swappable orchestrator
implementations:

> *"The Orchestrator is an exchangeable service. Naive version: forward
> all documents to the LLM in one call. Possible future versions: apply
> embedding, chunking, other 'smart' techniques, possibly multiple LLM
> calls."*

That promise no longer reflects how the project will evolve. We are not
going to add alternative orchestrator implementations. Instead, the
single `StandardOrchestrator` will grow by:

1. **Adding more use cases** as methods on the same class
   (`analyze`, `summarize`, `summarize_aggregate`, `assign_codes` today;
   more to come). Shared infrastructure (retry/deadline/anonymization)
   makes co-locating them on one application service the natural shape.
2. **Adding new driven adapters** behind new driven ports — for example,
   moving Presidio out of the orchestrator and behind an
   `AnonymizationPort`. These are *driven* ports the orchestrator uses,
   not alternative orchestrator implementations.

With the swappable-orchestrator requirement retracted, the original
ADR-008 trade-off no longer applies. `OrchestratorPort` is a *driving*
port — its only architectural job was to give driving adapters
(FastAPI route handlers) an abstraction to depend on instead of the
concrete `StandardOrchestrator`. Without the swap requirement, that
abstraction is decorative.

The architecturally load-bearing inversions are the **driven** ports
(`LLMPort` today, `AnonymizationPort` next). Those keep heavyweight
infrastructure (OpenAI SDK, Presidio's spaCy models) out of the
application ring and enable fast, mock-free testing. None of that
depends on `OrchestratorPort`.

## Decision

1. Drop the swappable-orchestrator requirement from the README.
2. Remove `OrchestratorPort` from `qfa.domain.ports`.
3. Type API dependencies and tests against `StandardOrchestrator`
   directly.

## Options Considered

### Option A: Keep `OrchestratorPort` (the ADR-008 position, now rejected)

- **Pro**: Three lines of code; documents the contract; preserves
  optionality.
- **Con**: The optionality it preserves is no longer wanted. A protocol
  with one permanent implementation is dead weight: it adds a layer of
  indirection that readers must traverse for no payoff.
- **Con**: Driving-port protocols implicitly suggest "this might be
  swapped" to future contributors. With no swap planned, the protocol
  miscommunicates intent.

### Option B: Remove `OrchestratorPort`, depend on the concrete service (chosen)

- **Pro**: One fewer abstraction. Route handlers and tests reference
  the class that actually exists.
- **Pro**: The hexagon is unaffected. `LLMPort` (and future
  `AnonymizationPort`) continue to invert the dependencies that matter
  — i.e. infrastructure stays outside the core.
- **Pro**: Adding new use cases as methods on `StandardOrchestrator`
  (or extracting one into its own class when it grows enough to earn
  its own collaborators) is unaffected by this decision.
- **Con**: If a second orchestrator ever materialises after all,
  reintroducing the protocol requires updating the route handler's
  type annotations, the dependency injection setup, and test
  fixtures. The refactor is small in lines but touches several files.
  This cost is judged acceptable given there is no concrete plan for a
  second implementation.

### Option C: Move `OrchestratorPort` to `qfa.services` (rejected)

- **Pro**: A driving port arguably belongs with the application layer
  it describes, not in the domain. This would clean up the
  driving/driven distinction in `domain/ports.py`.
- **Con**: Solves a categorisation issue without solving the
  underlying problem (an unwanted abstraction). Better to delete than
  to relocate.

## Consequences

- `qfa.domain.ports` continues to define **driven** ports only
  (`LLMPort`, and future `AnonymizationPort`). The driving/driven
  distinction in the package becomes consistent.
- `api/dependencies.py::get_orchestrator` returns `StandardOrchestrator`
  directly. Route handlers type-annotate against the concrete class.
- Tests no longer rely on a `FakeOrchestrator` satisfying a protocol.
  Where end-to-end orchestrator behaviour needs to be stubbed, tests
  either subclass `StandardOrchestrator` or — preferably — inject fake
  driven adapters (`FakeLLM`, `FakeAnonymizer`) and exercise the real
  orchestrator.
- The README's "exchangeable service" description is updated to reflect
  that the orchestrator is a single application service composed of
  multiple use cases, configured via swappable driven adapters rather
  than swappable orchestrator implementations.
- Adding a new use case requires only adding a method (or, when the
  use case is large enough, an additional class invoked by the
  orchestrator). No port changes.
- Adding a new driven adapter (e.g., `AnonymizationPort` for Presidio)
  is a separate decision tracked in its own ADR, independent of this
  one.

## When to revisit

- If a real, concrete need for an alternative orchestrator
  implementation emerges — for example, a chunking orchestrator that
  cannot live as another method on `StandardOrchestrator` because it
  has fundamentally different lifecycle, configuration, or shared
  state — supersede this ADR with a new one that reintroduces a
  driving port. The reintroduction cost is bounded and predictable.

## Participants

Marius
