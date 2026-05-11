# Architecture

How the service is structured and why. Roughly ordered C4-style — start at the top for the big picture and zoom in.

| Doc | When you need it |
|---|---|
| [Architecture style](01-architecture-style.md) | Hexagonal layout, layer rules, why we picked this pattern |
| [System context](02-system-context.md) | The app and its external neighbours (LLM provider, Presidio, Postgres, callers) |
| [Components](03-components.md) | Ports, adapters, the orchestrator, the composition root |
| [Cross-cutting concerns](04-crosscutting.md) | Anonymisation, tracking, error handling, logging — concerns that span layers |
| [Data model](05-data-model.md) | Domain models and persistence schema |

For decision history (why we chose what we chose), see the [architecture decision records](../adr/README.md).
