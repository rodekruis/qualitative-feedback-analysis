# ADR-008: Keep OrchestratorPort Despite Single Implementation

## Status

Superseded by [ADR-011](../011-drop-orchestrator-port.md)

## Context

The project currently has one orchestrator implementation
(`StandardOrchestrator`) — a naive proxy that forwards documents to the LLM
and returns the response. The README and project requirements explicitly
state that the orchestrator must be swappable:

> *"The Orchestrator is an exchangeable service. Naive version: forward all
> documents to the LLM in one call. Possible future versions: apply
> embedding, chunking, other 'smart' techniques, possibly multiple LLM
> calls."*

The devil's advocate challenged whether an abstract port is warranted when
only one implementation exists, arguing that a Python `Protocol` with one
concrete class is premature abstraction.

## Decision

Keep `OrchestratorPort` as a `typing.Protocol` in `domain/ports.py`.

## Options Considered

### Option A: Delete OrchestratorPort, depend on StandardOrchestrator directly (rejected)

- **Pro**: One fewer abstraction. Route handlers and tests reference the
  concrete class. Simpler mental model. If a second orchestrator never
  materializes, no abstraction cost was paid.
- **Con**: The project requirements explicitly promise swappable
  orchestrators. Introducing the port later requires changing the route
  handler's type annotations, the dependency injection setup, and all test
  fixtures. The refactor is small in lines of code but touches many files.
  More importantly, the `OrchestratorPort` protocol serves as documentation:
  it defines the contract any orchestrator must satisfy (deadline handling,
  error types, no partial results). Without it, these invariants are implicit
  in the `StandardOrchestrator` implementation and easy to violate in a
  future implementation.

### Option B: Keep OrchestratorPort as Protocol (chosen)

- **Pro**: The cost of a `Protocol` with one `async def` method is three
  lines of code. It documents the contract. Route handlers and dependencies
  type-annotate against the protocol, making the swappable-orchestrator
  promise real from day one. Tests inject fakes that satisfy the protocol.
  When a second orchestrator is added, no wiring changes are needed.
- **Con**: One extra type in the codebase with only one concrete
  implementation.

## Consequences

- `domain/ports.py` defines `OrchestratorPort` with the `analyze` method
  signature, including its error contract in the docstring.
- `api/dependencies.py` returns `OrchestratorPort` (not
  `StandardOrchestrator`) from `get_orchestrator`.
- Tests use a `FakeOrchestrator` that satisfies the protocol.
- Adding a new orchestrator (e.g., `ChunkingOrchestrator`) requires only:
  1. Implementing the protocol in a new class.
  2. Changing the wiring in `api/app.py::lifespan()` to select the
     implementation based on configuration.
  No route, dependency, or test changes needed.

## Participants

- Devil's advocate (proposed deletion)
- Architect (proposed keeping, accepted)
- Lead (resolved in favor of keeping — Protocol cost is negligible, the
  requirement is explicit in the README)
