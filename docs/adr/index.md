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
| [009](009-dedicated-state-storage-rg.md) | Dedicated resource group for Terraform state storage | Accepted |
| [010](010-shared-container-registry.md) | Shared container registry across environments | Accepted |
| [011](011-drop-orchestrator-port.md) | Drop swappable-orchestrator requirement and remove OrchestratorPort | Accepted (supersedes [008](obsolete/008-keep-orchestrator-port.md); decision 3 superseded by [017](017-orchestrator-composition-only.md)) |
| [012](012-postgres-mi-as-admin.md) | App Service managed identity as PostgreSQL Entra admin | Accepted |
| [013](013-keys-in-db-and-environment-vars.md) | Store API keys in both the database and environment variables | Accepted |
| [014](014-embedding-port-and-self-hosted-model.md) | EmbeddingPort and self-hosted BGE-M3 ONNX embedding model | Accepted |
| [015](015-hdbscan-clustering.md) | Cluster feedback records with HDBSCAN | Accepted |
| [016](016-guard-auto-deploy-on-publish.md) | Guard auto-deploy on release publish to the latest version only | Accepted |
| [017](017-orchestrator-composition-only.md) | Decompose the Orchestrator by composition only | Accepted (supersedes decision 3 of [011](011-drop-orchestrator-port.md)) |
| [018](018-no-third-party-text-in-error-envelope.md) | No third-party text in the error envelope | Accepted |
| [019](019-per-environment-app-service-plan-sizing.md) | Per-environment App Service plan sizing, prd on P0v3 | Accepted |
| [020](020-mistral-medium-as-judge-model.md) | `mistral-medium-3-5` as the judge model | Accepted |
| [021](021-split-cicd-identity.md) | Split the CI/CD identity: infra apply vs. image deploy | Accepted |

## Obsolete

| ADR | Title | Status |
|-----|-------|--------|
| [008](obsolete/008-keep-orchestrator-port.md) | Keep OrchestratorPort despite single implementation | Superseded by [011](011-drop-orchestrator-port.md) |

```{toctree}
:hidden:

001-pydantic-domain-models
002-protocol-based-ports
003-fully-async-concurrency
004-single-llm-client
005-bearer-auth
006-composed-settings
007-separate-api-schemas
009-dedicated-state-storage-rg
010-shared-container-registry
011-drop-orchestrator-port
012-postgres-mi-as-admin
013-keys-in-db-and-environment-vars
014-embedding-port-and-self-hosted-model
015-hdbscan-clustering
016-guard-auto-deploy-on-publish
017-orchestrator-composition-only
018-no-third-party-text-in-error-envelope
019-per-environment-app-service-plan-sizing
020-mistral-medium-as-judge-model
021-split-cicd-identity
obsolete/008-keep-orchestrator-port
```
