# Architecture

How the service is structured and why. Roughly ordered C4-style — start at the top for the big picture and zoom in.

| Doc | When you need it |
|---|---|
| [Architecture style](01-architecture-style.md) | Hexagonal layout, layer rules, why we picked this pattern |
| [System context](02-system-context.md) | The app and its external neighbours (LLM provider, Presidio, Postgres, callers) |
| [Components](03-components.md) | Ports, adapters, the orchestrator, the composition root |
| [Cross-cutting concerns](04-crosscutting.md) | Anonymisation, tracking, error handling, logging — concerns that span layers |
| [Data model](05-data-model.md) | Domain models and persistence schema |
| [Prompt envelope and guardrails](06-prompt-envelope.md) | Three-constant system message, XML-style envelope, escape helper, judge call contract for `POST /v1/analyze` |
| [Hierarchical analysis](07-hierarchical-analysis.md) | The `mode=hierarchical` embed → cluster → map → reduce algorithm for large corpora, in prose with flow and sequence diagrams |

For decision history (why we chose what we chose), see the [architecture decision records](../adr/index.md).

```{toctree}
:hidden:

01-architecture-style
02-system-context
03-components
04-crosscutting
05-data-model
06-prompt-envelope
07-hierarchical-analysis
/adr/index
```
