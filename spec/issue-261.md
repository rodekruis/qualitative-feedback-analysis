Part of the `Orchestrator` decomposition epic (#112). This is the **base** ticket: it
records the architecture decisions the other six children implement, so it lands first
and stays small.

## Spec

**What:** Add a new ADR (next free number: `docs/adr/017-orchestrator-composition-only.md`)
recording the three decisions taken during triage of #112, and annotate ADR-011 to point
at it.

**Why:** Six implementation PRs will follow, each written by a different agent or
developer at a different time. Without a written decision record, each one re-litigates
"should these services share a base class?" and the answers drift. The ADR is the
contract those PRs are reviewed against. It lands first, on its own, so it is cheap to
review and impossible to miss.

It also has to reconcile with existing ADRs: ADR-011 decided route handlers should type
against the concrete `StandardOrchestrator`, and this epic removes that class outright.
That relationship must be explicit rather than left for a reader to infer.

## Acceptance criteria

- [ ] `docs/adr/017-orchestrator-composition-only.md` exists with the standard section
      layout used by the other ADRs (Status / Context / Decision / Options Considered /
      Consequences / Participants), Status `Accepted`.
- [ ] Records decision 1: use-case services share behaviour via a concrete
      `LLMCallExecutor` collaborator injected into each; **no shared base class** among
      services.
- [ ] Records decision 2: the `Orchestrator` class is removed; each route handler
      depends on the single use-case service it needs.
- [ ] Records decision 3: `LLMCallExecutor` is a plain class, **not** a Protocol and
      **not** in `qfa.domain.ports`; service tests use the real executor over
      `FakeLLMPort` / `FakeAnonymizer`.
- [ ] States the resulting codebase-wide rule explicitly: inheritance is used **only**
      for declarative port↔adapter conformance (per ADR-002 and `AGENTS.md`); behaviour
      reuse is always composition.
- [ ] "Options Considered" covers and gives reasons for rejecting: (a) a shared abstract
      base service class, (b) keeping `Orchestrator` as a delegating facade, (c) keeping
      it as an attribute container, (d) declaring `LLMCallExecutor` as a Protocol port,
      (e) module-level free functions instead of a collaborator object.
- [ ] Explains the relationship to ADR-011: this is the "extract when it grows enough"
      path ADR-011 anticipated. ADR-011's decisions 1 and 2 (no swappable orchestrator,
      no `OrchestratorPort`) **stand**; only its decision 3 ("type API dependencies and
      tests against `StandardOrchestrator` directly") is superseded.
- [ ] `docs/adr/011-drop-orchestrator-port.md` gains a short pointer to ADR-017 noting
      that its decision 3 is superseded. ADR-011 is **not** moved to `obsolete/`.
- [ ] `docs/adr/index.md` (and `docs/adr/README.md` if it enumerates ADRs) lists
      ADR-017.
- [ ] No source code changes in this PR.
- [ ] `make lint` and `make docs` pass; the docs build produces no broken cross-references.
