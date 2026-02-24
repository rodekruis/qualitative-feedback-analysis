# ADR-006: Composed Settings with Environment Prefix Isolation

## Status

Accepted

## Context

The backend requires configuration for multiple concerns: LLM provider,
orchestrator tuning, authentication, and logging. All configuration comes
from environment variables (with `.env` file support for local development).

The question is whether to use a single flat settings class or multiple
composed sub-settings classes.

## Decision

Use composed sub-settings: `AppSettings` contains `LLMSettings`,
`OrchestratorSettings`, `AuthSettings`, and `LogSettings`, each with its
own `env_prefix`.

## Options Considered

### Option A: Single flat AppSettings (rejected)

- **Pro**: One class, one place to look. No nesting. Simple attribute access
  (`settings.openai_api_key`).
- **Con**: Environment variable names must be globally unique without
  namespacing. With 15+ settings, a flat class becomes hard to scan. Related
  settings (e.g., all retry parameters) are not visually grouped. Adding a
  new concern (e.g., caching settings) requires modifying the monolithic class.

### Option B: Composed sub-settings (chosen)

- **Pro**: Each settings group owns its `env_prefix` (`LLM_`, `AUTH_`,
  `ORCHESTRATOR_`). Environment variables are namespaced and self-documenting.
  Related settings are grouped. Each sub-settings class can be tested in
  isolation by setting only its prefixed env vars.
- **Con**: Accessing a setting requires one extra level: `settings.llm.model`
  instead of `settings.llm_model`. Developers must know which group a setting
  belongs to.
- **Mitigation**: The group names are intuitive (`llm`, `auth`, `orchestrator`).
  The env variable reference table in the architecture doc serves as a lookup
  aid.

## Consequences

- `settings.py` contains `LLMSettings`, `OrchestratorSettings`,
  `AuthSettings`, `LogSettings` (existing), and `AppSettings` (root).
- `AppSettings` uses `Field(default_factory=...)` for each sub-settings
  group, so each reads its own env prefix independently.
- Environment variables follow the pattern `{PREFIX}_{FIELD_NAME}`, e.g.,
  `LLM_API_KEY`, `AUTH_API_KEYS_CONFIG_PATH`.
- Adding a new settings group is self-contained: define a new sub-settings
  class and add it to `AppSettings`.

## Participants

- Architect (proposed composed settings)
- Devil's advocate (proposed flat, accepted composed as cost is low)
