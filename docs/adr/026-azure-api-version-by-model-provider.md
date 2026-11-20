# ADR-026: Pick the Azure API version from the model provider, `v1` for Azure OpenAI

## Status

Accepted

## Context

Every LLM call sends an Azure API version. Until this decision, Terraform
set one fixed value for every environment, `LLM_API_VERSION =
2024-05-01-preview`, and the judge connection inherited it (see
[ADR-020](020-mistral-medium-as-judge-model.md)).

Structured output (`response_format` with a JSON schema) exists in Azure
OpenAI from API version `2024-08-01-preview`. For an older version, LiteLLM
rewrites the request and sends the schema as a function tool instead.
LiteLLM routes model names containing `gpt-5` along a separate path that
always sends `response_format`, so `azure/gpt-5.4` never hit the rewrite.

On 2026-11-10, dev switched its primary model to `azure/gpt-6.1-sol`
(#468). That name is not on the `gpt-5` path, so LiteLLM sent a function
tool, and the model refused it: "Function tools with reasoning_effort are
not supported for gpt-6.1-sol in /v1/chat/completions". Every endpoint
returned 502 until `LLM_API_VERSION` was raised by hand on dev.

The API version was a hidden setting. Choosing a model did not show it,
and the judge silently inherited whatever the primary used, although the
right value depends on each connection's provider: `azure_ai/` models on
`/models` put it in the URL, while `azure_ai/` Claude on `/anthropic`
ignores it.

## Decision

The app picks the API version per connection from the model's LiteLLM
provider prefix (`default_api_version` in `qfa.adapters.llm_client`):

| Prefix | API version |
|---|---|
| `azure/` (Azure OpenAI) | `v1` (Azure's dateless API) |
| `azure_ai/` (Foundry `/models`, `/anthropic`) | `2024-05-01-preview` |
| anything else | none |

- `LLM_API_VERSION` and `JUDGE_LLM_API_VERSION` remain as overrides. The
  judge no longer inherits the primary's version.
- Terraform writes `LLM_API_VERSION` only when `var.llm_api_version` is
  set. Its default is empty, and no GitHub variable maps to it.
- The client removes a trailing `/` from the API base, because LiteLLM
  appends `/openai/v1/...` for `v1`.

## Alternatives considered

- **A per-environment `AZ_LLM_API_VERSION` GitHub variable.** Flexible, but
  every model switch would need the right version picked by hand, which is
  the knowledge that was missing in the incident.
- **Raising the shared dated version for every environment.** A new model
  could again need a newer date, and the judge would still inherit a value
  meant for another provider.
- **A model configuration file** listing every model with its endpoint
  type, price and per-environment purpose. Worth it when environments run
  many models. With one primary and one judge per environment it adds
  more than it removes. Reconsider if feature-specific models arrive in
  numbers.

## Consequences

- Choosing a model stays one GitHub variable per connection
  (`AZ_LLM_MODEL`, `AZ_JUDGE_LLM_MODEL`), with no version to choose.
- On the next apply, staging and prd move `azure/gpt-5.4` from
  `2024-05-01-preview` to `v1`. The request content is unchanged; only the
  Azure path changes, to `/openai/v1/chat/completions`. Staging receives it
  first with its release; prd only with Promote to prd.
- On 2026-11-12, prd also switched its primary model to `azure/gpt-6.1-sol`.
- The Mistral judge on staging and prd keeps `2024-05-01-preview`.
- A test in `tests/adapters/test_llm_client.py` sends a simulated request
  through LiteLLM and fails if structured output for `azure/gpt-6.1-sol` is
  sent as a tool again.
- Rollback: set `llm_api_version` in Terraform (or `LLM_API_VERSION` on the
  App Service) to the previous value.
