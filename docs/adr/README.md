# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for the
feedback analysis backend.

ADRs document significant architectural decisions, the context that led
to them, the options considered, and the reasoning behind the chosen approach.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [001](001-pydantic-domain-models.md) | Use Pydantic for domain models | Accepted |
| [002](002-protocol-based-ports.md) | Protocol-based ports instead of ABCs | Accepted |
| [003](003-fully-async-concurrency.md) | Fully async concurrency model | Accepted |
| [004](004-single-llm-client.md) | Single LLM client for all providers | Accepted |
| [005](005-bearer-auth.md) | Bearer token authentication | Accepted |
| [006](006-composed-settings.md) | Composed settings with env prefix isolation | Accepted |
| [007](007-separate-api-schemas.md) | Separate API schemas from domain models | Accepted |
| [008](008-keep-orchestrator-port.md) | Keep OrchestratorPort despite single implementation | Accepted |
