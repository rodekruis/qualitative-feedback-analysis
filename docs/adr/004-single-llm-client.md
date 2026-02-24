# ADR-004: Single LLM Client for All Providers

## Status

Accepted

## Context

The backend must support two LLM providers: OpenAI (direct) and Azure OpenAI.
Both use the `openai` Python SDK but with different client classes
(`AsyncOpenAI` vs `AsyncAzureOpenAI`) and different initialization parameters
(API key + base URL vs API key + Azure endpoint + API version).

The architect proposed two separate adapter classes: `OpenAILLMAdapter` and
`AzureOpenAILLMAdapter`, each implementing `LLMPort`.

## Decision

Use a single `LLMClient` class that accepts a pre-configured async OpenAI
client (`AsyncOpenAI` or `AsyncAzureOpenAI`) as a constructor argument.
Provider selection happens at startup in the composition root (`api/app.py`),
not inside the client.

## Options Considered

### Option A: Two separate adapter classes (rejected)

- **Pro**: Each adapter is self-contained and can be tested independently.
  Clear separation of provider-specific concerns.
- **Con**: The `complete()` method body is identical in both classes — same
  `client.chat.completions.create(...)` call, same exception mapping, same
  `store=False` and `user=tenant_id` enforcement. The only difference is
  constructor parameters. Maintaining two classes means two places to update
  when the call contract changes.

### Option B: Single class with injected client (chosen)

- **Pro**: One class, one `complete()` method, one place to enforce
  `store=False`. The `openai` SDK guarantees that `AsyncOpenAI` and
  `AsyncAzureOpenAI` have the same `chat.completions.create()` interface.
  Provider selection is a factory concern, not a client concern.
- **Con**: If the two providers diverge in their SDK interface (e.g., Azure
  adds a required parameter), the single class must handle the difference.
- **Mitigation**: The `openai` SDK maintainers have committed to interface
  parity between the two client classes. If they diverge, splitting the
  class at that point is a trivial refactor.

### Option C: Function instead of class (not chosen)

A plain `async def complete(client, ...)` function would work but makes it
harder to carry configuration (model name) without partial application or
closures. A class with `__init__` is clearer.

## Consequences

- `services/llm_client.py` contains one `LLMClient` class.
- `api/app.py` contains a factory function that reads `LLMSettings` and
  constructs either `AsyncOpenAI(api_key=..., ...)` or
  `AsyncAzureOpenAI(api_key=..., azure_endpoint=..., api_version=...)`,
  then passes it to `LLMClient(client=..., model=...)`.
- Tests mock the injected client object, not the `openai` module. This
  makes tests provider-agnostic.
- Adding a third provider (e.g., Anthropic, local model) requires a new
  adapter class that implements `LLMPort` — the single-client pattern does
  not extend to non-OpenAI-SDK providers. At that point, extract a second
  class.

## Participants

- Devil's advocate (proposed collapsing two classes into one)
- Architect (accepted — implementation bodies are identical)
