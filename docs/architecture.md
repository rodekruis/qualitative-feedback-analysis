# Architecture (moved)

The architecture documentation has been split into focused pages:

- [Architecture overview](architecture/01-architecture-style.md) — hexagonal layout, layers, why
- [System context](architecture/02-system-context.md) — the app and its external neighbours
- [Components](architecture/03-components.md) — ports, adapters, orchestrator, composition root
- [Cross-cutting concerns](architecture/04-crosscutting.md) — anonymisation, tracking, errors, logging
- [Data model](architecture/05-data-model.md) — domain models and persistence

The full doc index is at [the documentation home](README.md).

> The previous monolithic `docs/architecture.md` predated several major changes (LiteLLM, usage tracking, Presidio, ADR-011's removal of `OrchestratorPort`) and was significantly stale. The new pages replace it.
