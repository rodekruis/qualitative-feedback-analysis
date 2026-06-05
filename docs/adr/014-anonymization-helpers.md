# ADR-014: Anonymization Helpers in the Orchestrator

## Status

Accepted

## Context

Every flow in the `Orchestrator` (analyze, summarize, summarize\_aggregate,
detect\_sensitive\_content, \_pick\_code\_indices, \_judge\_code\_level) must protect PII
by anonymising user text before sending it to the LLM and de-anonymising the
result before returning it to the caller. The pattern was implemented
separately and manually in each method, leading to:

- Six copies of the same `if anonymize: … self._anonymizer.anonymize(…)` guard.
- Four copies of a three-line JSON round-trip deanonymisation block
  (`model_dump_json → deanonymize → model_validate_json`).
- A fragile coupling: any new flow must remember to repeat both blocks
  correctly or PII will be sent to the LLM or left un-restored in the response.

The issue asked for an elegant, architecture-safe way to centralise this
concern. Two preliminary ideas were proposed in the issue:

1. Extend `...ResultModel`s with a `sensitivestr` field type that is picked
   up automatically by the anonymiser.
2. Use method decorators to wrap each flow with the anonymise/deanonymise
   sandwich.

The constraint from the architecture documentation is that heavy integration
between the LLM port and the anonymisation port must be avoided.

## Decision

Extract two private helper methods directly on the `Orchestrator` class:

```python
def _anonymize_text(
    self, text: str, do_anonymize: bool
) -> tuple[str, dict[str, str]]: ...

def _deanonymize_model(
    self, model: _BaseModelT, mapping: dict[str, str]
) -> _BaseModelT: ...
```

`_anonymize_text` encapsulates the conditional `if anonymize:` guard around
`AnonymizationPort.anonymize`. When `do_anonymize` is `False` it returns the
original text and an empty mapping — so callers never need to branch.

`_deanonymize_model` encapsulates the JSON round-trip
(`model_dump_json → deanonymize → model_validate_json`) used to restore
placeholders in every result model. When the mapping is empty it short-circuits
and returns the model unchanged, avoiding an unnecessary serialisation.

The `analyze` flow's de-anonymisation step is intentionally left outside
`_deanonymize_model` because it operates on a plain `str` (not a Pydantic
model) and uses a filtered mapping that retains `<PERSON_*>` placeholders as a
defense-in-depth guardrail. This special case is small enough that a dedicated
helper would add more indirection than it removes.

## Options Considered

### Option A: `sensitivestr` field type in domain models (rejected)

Mark individual fields in `...ResultModel`s with a custom type
(`sensitivestr`) so the orchestrator (or a generic utility) can walk the model
and restore only those fields.

- **Pro**: Declarative. Each model self-describes which fields contain PII.
  No risk of accidentally restoring a non-PII field.
- **Con**: Couples domain models to the anonymisation concern. `models.py` is
  the innermost layer; adding infra-flavoured metadata to it violates
  ADR-001's intent (domain models are pure DTOs, not process orchestration).
- **Con**: Requires a bespoke Pydantic annotation type, a model-walker, and
  extra test coverage. For the actual payoff (selective field restoration), it
  over-engineers the problem: the current JSON round-trip is correct because
  placeholders can only appear in string fields the LLM filled in, and false
  positives are impossible given the `<TYPE_N>` placeholder format.
- **Con**: Does not help with the *input* side (anonymising `user_message`
  before the LLM call). Half the boilerplate remains.

### Option B: Method decorators (rejected)

Wrap each public orchestrator method with a decorator that anonymises the
request text, invokes the original method, and deanonymises the result.

- **Pro**: Invisible to each method body — the anonymisation concern is
  entirely outside the method.
- **Con**: The flows are not uniform. `analyze` has two LLM calls, uses a
  filtered mapping, and operates on a plain string result. `summarize_aggregate`
  has a multi-step pipeline (main call + judge call) where the judge must
  receive the *already-anonymised* user message. A single decorator cannot
  express these variations without becoming as complex as the problem it solves.
- **Con**: Decorators that mutate call arguments are hard to test and debug.
  The indirection makes it non-obvious which text the LLM receives.
- **Con**: Heavy integration between the LLM port and the anonymisation port is
  explicitly prohibited by the architecture documentation. A decorator that
  wraps LLM calls would create exactly this coupling.

### Option C: Private helper methods on the Orchestrator (chosen)

- **Pro**: Minimal change to the layering. Helpers live in `qfa.services`,
  the only layer that already knows about both the LLM port and the
  anonymisation port.
- **Pro**: The `if anonymize: / else:` branching collapses to a single
  unconditional call site in every flow.
- **Pro**: The JSON round-trip is defined once, tested once, and corrected once
  if the Pydantic API ever changes.
- **Pro**: New flows added in the future get the correct behaviour by calling
  the helpers — the right path is the easy path.
- **Con**: Does not prevent a future author from bypassing the helpers and
  writing the manual pattern again. Mitigated by documentation (this ADR)
  and code review.

## Consequences

- `Orchestrator._anonymize_text(text, do_anonymize) → (str, mapping)` and
  `Orchestrator._deanonymize_model(model, mapping) → model` are the canonical
  ways to handle the anonymise/deanonymise concern in any new flow.
- The `analyze` flow retains its bespoke deanonymisation for the `<PERSON_*>`
  retention guardrail.
- The `_BaseModelT` TypeVar (bound to `pydantic.BaseModel`) is module-private
  to `orchestrator.py`; it is not part of the public domain surface.
- Any new orchestrator method that sends user text to the LLM **must** call
  `_anonymize_text` for input and, where the result contains user-derived text,
  `_deanonymize_model` for output.

## Participants

- Architect (proposed helper methods as simplest correct approach)
- Devil's advocate (verified helpers earn their keep vs. inline code)
- Domain expert (confirmed PII handling correctness is preserved)
