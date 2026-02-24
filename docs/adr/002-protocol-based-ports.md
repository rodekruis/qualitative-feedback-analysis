# ADR-002: Protocol-Based Ports Instead of ABCs

## Status

Accepted

## Context

Hexagonal architecture defines ports as abstract interfaces that adapters
implement. Python offers two mechanisms for this:

1. `abc.ABC` with `@abstractmethod` — nominal subtyping. Implementations
   must explicitly inherit from the abstract class.
2. `typing.Protocol` — structural subtyping. Any class with matching method
   signatures satisfies the protocol without explicit inheritance.

The project has two ports: `LLMPort` (LLM provider interface) and
`OrchestratorPort` (analysis strategy interface). Each has exactly one
method.

## Decision

Use `typing.Protocol` for both `LLMPort` and `OrchestratorPort`.
Implementation explicitly inherit the Port protocol class.

## Options Considered

### Option A: abc.ABC (rejected)

- **Pro**: Explicit inheritance makes the relationship visible in the code.
  Forgetting to implement a method raises `TypeError` at class definition time.
- **Con**: Requires import of the ABC in every adapter module, creating a
  compile-time coupling between the infrastructure layer and the domain layer.
  For two single-method interfaces, the ceremony of `class MyAdapter(LLMPort):`
  adds no clarity over a matching method signature.

### Option B: typing.Protocol (chosen)

- **Pro**: Structural subtyping — any class with the right method signature
  satisfies the contract. No import needed in adapter code (though it can
  still be imported for documentation). Lighter, more Pythonic. Static type
  checkers (`mypy`, `ty`) verify conformance without runtime overhead.
- **Con**: No runtime `TypeError` if a method is missing — the error surfaces
  at call time or during type checking, not at class definition time.
- **Mitigation**: The project uses `ty` for type checking in CI. Conformance
  is verified on every push.

## Consequences

- Port definitions in `domain/ports.py` are `Protocol` classes.
- Adapter classes DO inherit from ports.
- Type checkers verify conformance. Tests verify behavior.
- Adding a new adapter requires matching the protocol's method signature.

## Participants

- Devil's advocate (proposed Protocol as lighter alternative)
- Architect (accepted — ABCs add no value for single-method interfaces)
