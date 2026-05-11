# Documentation

A short map of where things live.

## For developers

- [Architecture overview](architecture/01-architecture-style.md) — hexagonal layout, layers, pattern rationale
- [System context](architecture/02-system-context.md) — the app and its external neighbours
- [Components](architecture/03-components.md) — ports, adapters, orchestrator, composition root
- [Cross-cutting concerns](architecture/04-crosscutting.md) — anonymisation, tracking, errors, logging
- [Data model](architecture/05-data-model.md) — domain models and persistence
- [Architecture decision records](adr/README.md) — accepted ADRs
- [Ubiquitous language](ubiquitous_language.md) — domain vocabulary

## For operators

- [Operations index](operations/README.md) — table of every operations document
- [Deployment: runtime overview](operations/deployment.md) — container, migrations, multi-replica safety
- [Infrastructure bootstrap](operations/bootstrap.md) — one-time setup of the shared Terraform backend and container registry
- [Set up a new environment](operations/setup-new-env.md) — per-environment provisioning (`dev`, `staging`, `prd`)
- [API key management](operations/auth-management.md) — adding, rotating, and revoking keys
- [Settings reference](operations/settings-reference.md) — every environment variable
- [Observability](operations/observability.md) — logs, request tracing, usage queries

## For API consumers

- [API overview](api/README.md) — endpoint reference (live OpenAPI is served from `/docs` on a running instance)

## For integrators

- [EspoCRM connector scripts](integrations/espo-crm.md)

## Release notes

- [GitHub releases](https://github.com/rodekruis/qualitative-feedback-analysis/releases) — the source of truth for what shipped in each version
- [Migration guide for 0.14.0](migration/0.14.0-breaking-changes.md) — breaking field renames in 0.14.0
