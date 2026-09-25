# ADR-023: `PromptPort` and a push-based Langfuse prompt mirror

## Status

Accepted

## Context

#398 asks for system-prompt versioning: each experiment run against the
service should be assignable to the exact system prompt that produced it,
so offline evaluation can compare prompt versions against each other, not
just deployed commits.

Today every system prompt (`ANALYZE_SYSTEM_PROMPT`, `SYSTEM_PROMPT` in
`coding_classifier.py`, and their siblings across `analyze.py`,
`hierarchical_prompts.py`, `summarize.py`, `coding_classifier.py`, and
`sensitivity.py`) is a hardcoded Python string constant, reviewed and
versioned in this repo's own git history like any other code. Langfuse
separately offers a Prompt Management feature: named, versioned prompts,
fetchable at runtime and linkable to the traces a version produced.

Two designs were on the table. Either move prompt text into Langfuse and
fetch it at call time, or keep the repo as ground truth and only mirror
each prompt's current text to Langfuse for version history and trace
linking. Olaf chose the second, after this ADR's author verified that
`langfuse==4.15.2`'s `create_prompt` always creates a new version on every
call, with no content-based deduplication, which the first design would
need to avoid spamming a new version on every deploy.

## Decision

1. **Prompt text stays hardcoded in Python.** Nothing in this feature
   fetches prompt text from Langfuse at request time, and no call site's
   behavior changes. `qfa.services.prompt_registry.SYSTEM_PROMPTS` is the
   one place every one of the 11 versioned prompts is named, each value
   imported from its owning module, never copied as a duplicate literal.

2. **One combined, static Langfuse prompt per composed flow**, not one
   per constituent constant. Where a real system message is assembled
   from several constants (for example `ANALYZE_SYSTEM_PROMPT` +
   `ANALYZE_GUARDRAILS_PROMPT` + `ANALYZE_ACTION_PROMPT`), the registry
   holds the combined static text as one named entry, so one generation
   links to exactly one prompt version. A per-call dynamic suffix, such
   as the output-language instruction, is excluded, since it belongs to
   one request, not to the prompt's identity.

3. **A new port, `PromptPort`**, declared in `qfa.domain.ports` as a
   `Protocol` per ADR-002, with one async method,
   `sync(prompts: Mapping[str, str]) -> dict[str, int]`. Async because a
   real implementation does blocking network I/O, unlike the synchronous
   `EvaluationPort`, but it runs once, at startup, never on the request
   path.

4. **Two adapters, a real null object**, mirroring ADR-022's pattern for
   `EvaluationPort`. `qfa.adapters.prompts.LangfusePromptAdapter` talks to
   Langfuse. `NoOpPromptAdapter` returns `{}` unconditionally, the default
   while `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are unset. The
   composition root always builds one or the other, so no call site
   branches on whether Langfuse is configured.

5. **`create_prompt` always creates a new version, so the adapter
   compares first.** Reading the installed SDK source, and the Langfuse
   community's own open feature request for hash-based deduplication,
   confirmed there is no dedup in either the client or the server.
   `LangfusePromptAdapter.sync` therefore calls
   `get_prompt(name, label="production")` for each name, catches
   `langfuse.api.NotFoundError` to mean "no version yet", and only calls
   `create_prompt` when no version exists or the existing version's text
   differs from the current hardcoded constant.

6. **Trace linking is two OTel span attributes, not a Langfuse SDK
   call.** `LLMPort.complete` and `LiteLLMClient.complete` gain two
   optional keyword arguments, `prompt_name` and `prompt_version`.
   `LiteLLMClient.complete` sets `langfuse.observation.prompt.name` and
   `langfuse.observation.prompt.version` on its existing span when both
   are given, the same literal-string-attribute style the rest of that
   method already uses. `LiteLLMClient` holds no prompt-version state of
   its own; each service passes its own flow's name and the version it
   looked up from the shared `prompt_versions` dict it was constructed
   with.

7. **`build_services` becomes `async def`.** `build_prompt_versions` is
   async, and is called once inside `build_services` unless a caller
   overrides it via a new `prompt_versions: dict[str, int] | None = None`
   parameter, matching the existing `evaluator` override. Every test and
   script that called `build_services`/`build_analyze_service`
   synchronously needed the same one-line `await` this decision implies.

8. **`GET /v1/health` gains a `prompts: dict[str, int]` field**, current
   version per name, sourced from the same `prompt_versions` dict every
   service already holds, and published on `app.state.prompt_versions` at
   startup. It never carries prompt text: the endpoint is effectively
   unauthenticated, and a guardrail or system-role prompt leaking there
   would be a real disclosure.

9. **The offline eval scripts report the prompt version next to the
   score it produced.** `eval/_common.py` gains `prompt_versions(base_url)`,
   reading the new health field the same way `deployed_info` already
   reads `version`/`commit`. `assign_codes_eval.py` and
   `evaluate_sensitivity.py` each pass only the one or two names they
   actually score into `run_metadata()`'s existing `**extra`.

## Options considered

### Fetch prompt text from Langfuse at runtime (rejected)

This is what "Langfuse Prompt Management" usually means: `get_prompt`
returns the live text, compiled with `{{variable}}` substitution, and
every deploy stays in sync with whatever the Langfuse UI shows as
`production`. Rejected because it moves ground truth out of the
repository's own review and git history, adds Langfuse reachability to
the request path (a new failure mode this decision explicitly avoids, see
point 5), and was not what Olaf asked to keep.

### Rely on `create_prompt`'s own deduplication (rejected, does not exist)

The original framing of this feature assumed pushing a prompt on every
startup would be a harmless no-op when the text had not changed.
Verifying the installed SDK's source showed this is false: every call
creates a new version, unconditionally. Building `LangfusePromptAdapter`
around this wrong assumption would have created a new, meaningless
version on every restart and every deploy, defeating the entire point of
comparing experiments by prompt version.

### One Langfuse prompt per constituent constant (rejected)

Pushing `ANALYZE_SYSTEM_PROMPT`, `ANALYZE_GUARDRAILS_PROMPT`, and
`ANALYZE_ACTION_PROMPT` as three separate named prompts would give
finer-grained history, but one generation would then link to three prompt
versions instead of one, which weakens exactly the comparison view #398
asks for.

## Consequences

- `qfa.domain.ports.PromptPort`, `qfa.adapters.prompts.LangfusePromptAdapter`,
  `qfa.adapters.prompts.NoOpPromptAdapter`,
  `qfa.services.prompt_registry.SYSTEM_PROMPTS`, and
  `qfa.api.composition.build_prompt_versions` are new.
- `qfa.api.composition.build_services` and
  `qfa.api.composition.build_analyze_service` are now `async def`. Every
  caller, in production code, tests, and the one notebook that calls
  `build_analyze_service`, needed a new `await`.
- `AnalyzeService`, `SummarizeService`, `CodingService`, and
  `SensitivityService` each take a `prompt_versions: dict[str, int] | None`
  constructor argument, defaulted to `None`.
- `LLMPort.complete`, `LiteLLMClient.complete`,
  `LLMCallExecutor.complete`, and `LLMCallExecutor.bounded_complete` each
  gain `prompt_name`/`prompt_version` optional keyword arguments. Every
  fake `LLMPort` in the test suite needed the same two, unused,
  parameters.
- `ApiHealthResponse` gains `prompts: dict[str, int]`, and `health()`
  gains a `Request` parameter to read `app.state.prompt_versions`.
- The import-linter "Enforce hexagonal layers" contract needed one new
  `ignore_imports` line, `qfa.api.composition -> qfa.adapters.prompts`,
  matching the existing evaluator and embedder entries.
- `eval/_common.py` gains `prompt_versions()`, and both
  `assign_codes_eval.py` and `evaluate_sensitivity.py` add one
  `prompt_version` key to their `run_metadata()` call.

## When to revisit

- If Langfuse ships server-side deduplication for `create_prompt`, the
  compare-then-create logic in `LangfusePromptAdapter.sync` (point 5)
  becomes redundant and can be simplified.
- If a twelfth prompt is added, or an existing flow's composition
  changes, `qfa.services.prompt_registry.SYSTEM_PROMPTS` and the flow
  table in `SPEC.md` are the two places to update together.
- If prompt text ever needs to vary per tenant or per experiment at
  request time, this ADR's core decision, that the repository stays the
  only source of truth, needs revisiting first.

## Participants

Olaf
