# SPEC: Prompts in Langfuse (closes #398)

Status: Olaf approved this spec during a grilling session in conversation.
It is ready for agent-skills:planning-and-task-breakdown and
agent-skills:build.

## 1. Objective

System prompts have no version history today. Issue #398 asks for one.
Without a version history, an offline evaluation run can only name the git
commit that ran. It cannot name the exact prompt text that produced the
result.

This feature mirrors every hardcoded system prompt to Langfuse Prompt
Management. Each prompt then gets a real version number in Langfuse.
Every LLM generation trace carries the prompt name and version that
produced it. The offline evaluation scripts read those version numbers
too. The prompt text itself stays in the repository as the source of
truth.

Out of scope: a runtime fetch of prompt text from Langfuse. The
repository stays the only source of truth for prompt text. This feature
does not change what text goes to the LLM. It does not change any public
API response contract, except for one new field on the health endpoint
(see section 4.6).

## 2. Verified facts

Do not re-derive these facts. Do not guess new facts that conflict with
them.

- `langfuse==4.15.2` is already installed. `pyproject.toml` already
  declares `langfuse>=4.6` as a dependency.
- `Langfuse.create_prompt(*, name, prompt, labels=[], ...)` always creates
  a new version on every call. Neither the server nor the client looks
  for unchanged content first. Reading the installed SDK source confirms
  this.
  SDK source. The Langfuse community has an open feature request for
  hash-based deduplication, and Langfuse has not built it yet. If this code
  calls `create_prompt` on every app startup with no comparison first, it
  creates a new version on every restart. This happens even without a
  real change. For this reason, the adapter must compare the text first.
  When the text differs, the adapter must call `create_prompt`.
- `Langfuse.get_prompt(name, *, label=None, version=None, ...)` returns a
  `TextPromptClient` object. That object carries `.name` (a string),
  `.version` (an integer), and `.prompt` (a string). When the name does
  not exist yet, `get_prompt` raises `langfuse.api.NotFoundError`, a
  subclass of `langfuse.api.core.api_error.ApiError` with
  `status_code=404`. Catch this exact exception to detect a first push
  for a given name.
- The Langfuse SDK's own OTel span attribute names for prompt linking are
  `langfuse.observation.prompt.name` and
  `langfuse.observation.prompt.version`. Reading
  `langfuse/_client/attributes.py` confirms both names. Use these two
  literal strings directly in `qfa`. This matches how `llm_client.py`
  already hardcodes strings such as `langfuse.observation.model.name`. Do
  not import anything from `langfuse._client`, because that module is
  private.
- `ApiHealthResponse` (`src/qfa/api/schemas.py:1229-1241`) has three
  fields today: `status`, `version`, and `commit`. `health()`
  (`src/qfa/api/routes.py:651-666`) builds these from `qfa.__version__`
  and `os.environ.get("GIT_SHA", "unknown")`. It does not read
  `app.state` today.
- Ports under `qfa.domain.ports` are `typing.Protocol` classes, not ABCs,
  per ADR-002. Concrete adapters and test fakes still declare the port as
  a base class, following AGENTS.md's port-inheritance rule. Protocol
  conformance does not need this declaration. This codebase picks sync or
  async per port by whether the real implementation blocks on a network
  call. `EvaluationPort` and `EmbeddingPort` are sync, because their real
  implementation never blocks. `LLMPort` and similar ports are async,
  because their real implementation does real network I/O.
- `build_evaluator(settings: LangfuseSettings) -> EvaluationPort` is the
  closest existing match for this feature. It looks at whether
  `settings.public_key` and `settings.secret_key` are set. When they are
  not set, it logs a message and returns a Null Object
  (`NoOpEvaluationAdapter`). When they are set, it builds the real
  adapter. `build_services` calls `build_evaluator` on its own, and also
  accepts an `evaluator: EvaluationPort | None = None` override for
  tests. This feature must follow the same pattern for prompts.
- `LangfuseEvaluationAdapter.__init__` builds its own `Langfuse` client,
  with its own `TracerProvider()`. It never reuses the process-global
  tracer provider. This feature must follow the same pattern.

## 3. Prompt inventory

This feature manages exactly 11 named prompts. Each name maps to one
combined, static Langfuse text prompt. Each of these 11 prompts matches
one distinct composed system message in the code today.

Some system messages append dynamic text at call time. Two examples are
the output-language instruction, and `summarize_bulk`'s free-text
`request.prompt` override. Exclude this dynamic text from the versioned
prompt, because it is specific to one request, not part of the prompt's
identity.

Some prompts are Python `.format()` templates, with placeholders such as
`{source_text}`. Push each of these templates to Langfuse unfilled, as
plain text. Langfuse never renders these templates, so do not convert the
Python placeholders to Langfuse's own `{{mustache}}` syntax.

| Langfuse name | Source constant(s) | File |
|---|---|---|
| `analyze-single-pass-system` | `ANALYZE_SYSTEM_PROMPT` + `ANALYZE_GUARDRAILS_PROMPT` + `ANALYZE_ACTION_PROMPT` | `services/prompts.py`, used in `services/analyze.py:273-277` |
| `analyze-hierarchical-map-system` | `ANALYZE_SYSTEM_PROMPT` + `ANALYZE_GUARDRAILS_PROMPT` + `_MAP_ACTION_PROMPT` | `services/hierarchical_prompts.py` |
| `analyze-hierarchical-reduce-system` | `ANALYZE_SYSTEM_PROMPT` + `ANALYZE_GUARDRAILS_PROMPT` + `_REDUCE_ACTION_PROMPT` | `services/hierarchical_prompts.py` |
| `analyze-judge` | `ANALYZE_JUDGE_PROMPT` (raw, unfilled) | `services/prompts.py` |
| `summarize-aggregate-system` | `_DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT` | `services/summarize.py`, near line 234 |
| `summarize-single-system` | `_DEFAULT_SUMMARIZATION_PROMPT` | `services/summarize.py`, near line 332 |
| `summarize-community-meeting-system` | `_DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT` | `services/summarize.py`, near line 400 |
| `summarize-judge` | `_JUDGE_PROMPT` (raw, unfilled) | `services/summarize.py` |
| `coding-classifier-system` | `SYSTEM_PROMPT` (`_SYSTEM`) | `services/coding_classifier.py` |
| `coding-classifier-judge` | `_JUDGE_SYSTEM` | `services/coding_classifier.py` |
| `sensitivity-detection-system` | `_DEFAULT_SENSITIVITY_DETECTION_PROMPT` | `services/sensitivity.py` |

Before you migrate a prompt, read its file. Make sure that you have the
constant's exact current text. Line numbers in this table are
approximate. Names in this table are fixed.

## 4. Architecture

### 4.1 New port: `src/qfa/domain/ports.py`

```python
class PromptPort(Protocol):
    """Port for mirroring hardcoded system prompts to Langfuse for versioning.

    The repo's hardcoded prompt text is always the ground truth. This port
    never feeds text back into a call. ``sync`` is async because a real
    implementation does blocking network I/O, unlike EvaluationPort. It
    runs once, during app startup, before the server accepts traffic. It
    never runs on the request path.
    """

    async def sync(self, prompts: Mapping[str, str]) -> dict[str, int]:
        """Make sure every name in ``prompts`` exists in Langfuse at that exact text.

        Follow this rule for each ``name: text`` pair. When no version
        exists yet, or the current version's text differs from ``text``,
        create a new Langfuse version. Return ``{name: current_version}``
        for every key in ``prompts``, whether or not this call created a
        new version.
        """
        ...
```

### 4.2 New adapters: `src/qfa/adapters/prompts.py`

`LangfusePromptAdapter(PromptPort)`:

- `__init__(self, settings: LangfuseSettings)` builds its own
  `Langfuse(...)` client, the same way `LangfuseEvaluationAdapter` does.
- `sync` calls `self._client.get_prompt(name, label="production")` for
  each `name, text` pair. It catches `langfuse.api.NotFoundError` to mean
  "this name does not exist yet". When the name does not exist, or when
  `existing.prompt != text`, it calls
  `self._client.create_prompt(name=name, prompt=text, labels=["production"])`
  and reads `.version` from the result. Otherwise it reads `.version`
  from the existing prompt. It collects and returns `{name: version}`.
- When one prompt's lookup or create call raises any other exception,
  `sync` must not stop the others. Log a warning that names the prompt.
  Never log one that carries the prompt text. Leave that name out of the
  returned dict. Section 8 has the full logging rule.

`NoOpPromptAdapter(PromptPort)`:

- `sync(self, prompts)` returns `{}` for any input. When Langfuse is not
  configured, this code uses this Null Object, the same way
  `NoOpEvaluationAdapter` works today. Do not invent placeholder version
  numbers.

### 4.3 New registry module: `src/qfa/services/prompt_registry.py`

```python
SYSTEM_PROMPTS: dict[str, str] = {
    "analyze-single-pass-system": ...,
    # all 11 names from section 3, each imported from its owning module,
    # never copied as a duplicate string
}
```

This is the one list every consumer reads: the startup push, and the
tests. Adding a twelfth prompt later means adding one entry here.

### 4.4 Composition wiring

Add one new function to `src/qfa/api/composition.py`, matching
`build_evaluator`:

```python
async def build_prompt_versions(settings: LangfuseSettings) -> dict[str, int]:
    if settings.public_key is None or settings.secret_key is None:
        logger.debug("LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY unset, prompt sync disabled")
        return await NoOpPromptAdapter().sync(SYSTEM_PROMPTS)
    logger.info("Langfuse prompt sync configured (host=%s)", settings.host)
    return await LangfusePromptAdapter(settings).sync(SYSTEM_PROMPTS)
```

Call this function once, inside `build_services`, in the same place
`build_evaluator` is called today. Add a new
`prompt_versions: dict[str, int] | None = None` override parameter on
`build_services` for tests, matching the existing `evaluator` override.
Add a `prompt_versions: dict[str, int]` field on `ServiceGraph`, so the
lifespan can read the result once and reuse it for
`app.state.prompt_versions`. Call `build_prompt_versions` only once. One
network round trip happens at startup, one dict is shared everywhere, and
no two copies can ever disagree.

`AnalyzeService`, `SummarizeService`, `CodingService`, and
`SensitivityService` each receive the same full
`prompt_versions: dict[str, int]` in their constructor, next to
`evaluator`. Each service looks up only the names it needs from that one
dict.

Do not change `build_llm_client`'s signature. Do not change the
`LLMFactory` type alias (`Callable[[LLMSettings], LLMPort]`).
`build_llm_client` and `llm_factory` stay exactly as they are today.
Prompt versions reach `LiteLLMClient` only as data passed into each
`complete()` call (see section 4.5), never through the client's
constructor.

### 4.5 Trace linking

`src/qfa/domain/ports.py` and `src/qfa/adapters/llm_client.py`:

`LLMPort.complete` and `LiteLLMClient.complete` each gain two new
optional keyword parameters: `prompt_name: str | None = None` and
`prompt_version: int | None = None`. When both parameters are not `None`,
add these two lines inside `LiteLLMClient.complete`'s existing span block
(`src/qfa/adapters/llm_client.py:538-577`):

```python
span.set_attribute("langfuse.observation.prompt.name", prompt_name)
span.set_attribute("langfuse.observation.prompt.version", prompt_version)
```

`LiteLLMClient` holds no prompt-version state of its own. It only tags
whatever the caller passes. This keeps it decoupled from Langfuse's
prompt API. It already never talks to Langfuse directly today. The OTel
exporter does that work instead.

Each service call site passes its own flow's name and version. For
example, in `analyze.py`:

```python
self._llm.complete(
    system_message=system_message,
    ...,
    prompt_name="analyze-single-pass-system",
    prompt_version=self._prompt_versions.get("analyze-single-pass-system"),
)
```

In two cases, `self._prompt_versions.get(name)` returns `None`. One case
is that Langfuse is not configured. The other case is that entry's push
failed. When `prompt_version` is `None`, `complete()` must skip both new
attributes. Never send a name without a version, and never send a
version without a name.

### 4.6 Health endpoint

`ApiHealthResponse` (`src/qfa/api/schemas.py`) gains one field:

```python
prompts: dict[str, int] = Field(
    description="Current Langfuse prompt version for each system prompt "
    "name. Never the prompt text itself."
)
```

`health()` (`src/qfa/api/routes.py`) needs a new `request: Request`
parameter, so it can read `request.app.state.prompt_versions`. It returns
that dict as-is for the new field. When Langfuse is not configured, this
field is `{}`. It is never a dict of invented zero values.

### 4.7 Offline evaluation scripts

`eval/_common.py`'s `run_metadata(base_url, **extra)` needs no change. It
already accepts any extra key.

`eval/assign_codes_eval.py` fetches `GET /v1/health` and reads
`health["prompts"]["coding-classifier-system"]`. Read the script first to
know what it scores. If it also scores the per-level judge output, it
also reads `health["prompts"]["coding-classifier-judge"]`. It passes
`prompt_version=...` into `run_metadata()`.

`eval/evaluate_sensitivity.py` does the same, reading
`health["prompts"]["sensitivity-detection-system"]`.

Update `eval/README.md`'s "Shared helpers" table with the new key or
keys.

## 5. Build order

Keep commits small. Build in this order.

1. Add `PromptPort`, `LangfusePromptAdapter`, `NoOpPromptAdapter`,
   `prompt_registry.py`, and the `build_prompt_versions` wiring into
   `build_services` and `ServiceGraph`. No caller reads the result yet.
2. Add the `prompts` field to `GET /v1/health`, sourced from
   `app.state.prompt_versions`.
3. Migrate each of the 11 prompt flows. For each one, thread
   `prompt_name` and `prompt_version` from the service's call site
   through to `LiteLLMClient.complete`, and add the two new span
   attributes in `llm_client.py`. Use one commit per flow. If a small
   group of flows stays reviewer-sized as one commit, group them instead.
4. Update `eval/assign_codes_eval.py`, `eval/evaluate_sensitivity.py`, and
   `eval/README.md`.

## 6. Testing strategy

- Test `LangfusePromptAdapter.sync` by mocking the `Langfuse` client, the
  same way `TestBuildEvaluator` and `TestBuildLangfuseTracer` already do
  in `tests/api/test_composition.py`. Cover these four cases:
  - A name that does not exist yet calls `create_prompt` with
    `labels=["production"]`.
  - A name that exists with identical text does not call `create_prompt`.
  - A name that exists with different text calls `create_prompt` and
    returns the new version.
  - One name's lookup or create call raises an unrelated exception. This
    does not stop the others.
- Test that `NoOpPromptAdapter.sync` returns `{}` for any input.
- Test the gate inside `build_prompt_versions`. Settings with no keys
  route to `NoOpPromptAdapter`. Settings with both keys route to
  `LangfusePromptAdapter`. `TestBuildEvaluator` tests this same gate
  today.
- Test `build_services`'s new `prompt_versions` override. Add a
  `class FakePromptPort(PromptPort):`, following AGENTS.md's rule that
  test doubles declare the port explicitly. If the specific test file
  already favors a plain stub instead, as `test_composition.py`'s
  `_StubEvaluator` does today, add a plain `_StubPromptPort` there
  instead.
- Extend `tests/adapters/test_llm_client.py` to test
  `LiteLLMClient.complete`. When both `prompt_name` and `prompt_version`
  are given, make sure that it sets the two new span attributes. When
  either argument is `None`, make sure that it sets neither one.
- Extend `tests/api/test_routes.py`'s `TestHealth` class with one case
  that makes sure `data["prompts"]` matches `app.state.prompt_versions`.
- The `eval/` script changes need no test. `eval/README.md` already
  states that `eval/` sits outside `make test`. Only its documentation
  needs an update.

## 7. Documentation

- Add a new ADR at `docs/adr/023-....md`, the next free number after the
  existing 001 through 022. Copy ADR-022's exact section order: `Status`,
  `Context`, `Decision`, `Options considered`, `Consequences`, `When to
  revisit`, and `Participants`. Document three things: prompt text stays
  hardcoded as ground truth, Langfuse holds a one-way, push-based mirror
  for versioning, and the new `PromptPort`. Also document the fact from
  section 2 that `create_prompt` always creates a new version, and how
  the adapter works around that. Add the new ADR to `docs/adr/index.md`.
- In `docs/operations/observability.md`, find the existing `## Langfuse
  tracing` section. Add the two new span attributes to its existing
  bullet list titled "Each span carries these attributes".
- Do not touch `docs/security-brief.html`. Nothing security-relevant
  changes in this feature. No new secret appears, and no new attack
  surface opens: the health endpoint exposes only integers, never prompt
  text.

## 8. Boundaries

Always do these things. Gate every part of the Langfuse prompt-sync
behavior on `LangfuseSettings` being configured. Follow the same pattern
the existing tracer and evaluator already use. Keep prompt text hardcoded
in Python as the ground truth. When a sync call fails, log the failure
and continue. Never raise from a sync failure. App startup must not
depend on Langfuse being reachable. Catch `langfuse.api.NotFoundError` by
name for the "not found" case. Never catch a bare `Exception` for that
one case.

Never do these things: fetch prompt text from Langfuse at request time.
Return prompt text from `GET /v1/health`. Change `build_llm_client`'s
signature or the `LLMFactory` type alias. Change any public API response
schema other than `ApiHealthResponse`. Touch `docs/security-brief.html`
without an actual security-relevant change behind it.

Ask first about anything not listed above. Olaf resolved every open
question already, in the conversation that produced this spec. If
`/build` finds a real gap this spec does not cover, stop and ask instead
of guessing.
