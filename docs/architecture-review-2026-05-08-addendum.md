# Architecture Review Addendum — 2026-05-08

**Companion to:** [`architecture-review-2026-05-08.md`](architecture-review-2026-05-08.md)
**Reviewer:** Claude (Opus 4.7)
**Scope:** the three caveats from the first review's "What this review
does *not* claim" section, plus a few things that fell out of reading
those areas:

1. The ADRs (`docs/adr/`) — read all 11 accepted + the obsolete-008.
2. The skipped API files (`api.dependencies`, `api.routes_usage`,
   `api.schemas`, `api.schemas_usage`, `cli.migrate`).
3. Selected test fixtures — `tests/e2e/conftest.py`, `tests/api/conftest.py`,
   `tests/services/test_orchestrator.py`, plus a test run.

I ran `pytest` for the non-DB suites. **190 tests pass in 7.93s.**

---

## TL;DR for the addendum

| Area | Verdict |
|---|---|
| **ADRs as a body of work** | High-quality decision-making with one exemplary self-correction (ADR-007's 2026-04-29 amendment) and one good supersession (ADR-011 → ADR-008). Two ADRs have outdated *implementation* descriptions even though their *decisions* still hold (ADR-002, ADR-004). |
| **`docs/architecture.md`** | **Significantly stale.** It still describes `OrchestratorPort` (removed by ADR-011), `services/llm_client.py` (now `adapters/`), `AsyncOpenAI`-backed clients (now LiteLLM), `LLMResponse.text` (now `structured`), and a stateless service (now backed by Postgres for usage tracking). Endpoints listed: 2 of the 6 that exist today. |
| **Test fakes** | **Two real bugs** in test fakes that survived because they're either never called (dead code) or because pydantic v2 silently drops extra fields. The most concerning: `tests/e2e/conftest.py::FakeLLMPort.complete()` has a signature that doesn't match the current `LLMPort.complete()` — orchestrator's kwarg call would TypeError on `response_model`. |
| **Unfinished `ApiCodingNode` migration** | The typed coding-framework model exists in `api/schemas.py` (with a depth-validator and 13 unit tests), but `ApiAssignCodesRequest.coding_framework` still uses `dict[str, Any]`. Someone built the right thing and never wired it in. |
| **Ubiquitous language drift** | The UL doc declares a careful IFRC-aligned vocabulary ("feedback record", "community member", avoid "document"/"beneficiary"). The code uses `FeedbackItemModel`, the analyze API exposes a `documents` field, and the LLM system prompts repeatedly say "beneficiary feedback". |
| **API field-name inconsistency** | The same conceptual entity is `documents[].text` on `/v1/analyze`, `feedback_items[].content` on `/v1/summarize` and `/v1/assign_codes`. Different names for the same thing across endpoints. |
| **Migrations / e2e / composition** | Genuinely well-designed. Multi-replica-safe migration via pg advisory lock. E2E tests boot the real composition root via `LifespanManager` with the fake LLM injected through `create_app(llm_factory=...)` — exactly the pattern hex enables. **Positive callouts.** |

---

## 1. ADRs — close reading

The `adr/` directory contains 11 accepted ADRs + 1 obsoleted by
ADR-011. Reading them changed several conclusions from the first
review.

### ADR-001 (Pydantic in domain) — accepted, applies as written

The "Pydantic leaks into the domain" observation in the first review
was **not** a new finding — ADR-001 explicitly chose `Pydantic
BaseModel(frozen=True)` for the domain after considering frozen
dataclasses with a translation layer. The trade-off is documented; a
migration path exists if a future requirement demands a framework-free
domain. My original "soft architectural leak" framing should have been
"deliberate choice, see ADR-001."

`★ Insight ─────────────────────────────────────`
- ADR-001 is a clean example of the "skip the abstraction layer when
  the layer it would protect against is itself stable" decision. Pydantic
  v2 has a stable API; it's not a web framework; the cost of swapping it
  out (rewrite domain models as dataclasses, add a translation layer) is
  bounded, predictable, and only paid if the requirement actually
  materializes. Most projects fall into the trap of building the
  translation layer "in case" — this project explicitly didn't and wrote
  the migration recipe instead.
`─────────────────────────────────────────────────`

### ADR-002 (Protocol over ABC for ports) — internal inconsistency

This ADR has a real **internal contradiction** worth fixing.

The "Con" of `abc.ABC`:
> Requires import of the ABC in every adapter module, creating a
> compile-time coupling between the infrastructure layer and the
> domain layer. […] the ceremony of `class MyAdapter(LLMPort):` adds
> no clarity over a matching method signature.

The "Pro" of `typing.Protocol`:
> No import needed in adapter code (though it can still be imported
> for documentation). Lighter, more Pythonic.

But under "Consequences":
> **Adapter classes DO inherit from ports.** Type checkers verify
> conformance. Tests verify behavior.

So the project rejected ABC because of the cost of explicit
inheritance, then chose Protocol — and then mandated explicit
inheritance anyway. The "lighter ceremony" advantage that justified
choosing Protocol is voluntarily forfeited in production code.

**The actual rationale for choosing Protocol over ABC** (per
`AGENTS.md`):

> Although Python `Protocol`s support structural typing without
> inheritance, the explicit base class makes the port↔adapter
> relationship discoverable in IDEs ("go to definition" jumps to the
> contract) and signals intent to readers. **Structural conformance is
> reserved for ad-hoc test fakes that don't need to be navigable as
> port implementations.**

That's the right reason — *production adapters get nominal typing for
discoverability; test fakes get structural typing to avoid a noisy
mock setup.* Best-of-both. But ADR-002 doesn't say this. It frames
Protocol as "lighter" without acknowledging that the project doesn't
take the lightness in production.

**Recommendation:** amend ADR-002 to capture the actual
production/test-fake split that `AGENTS.md` documents. Clarify that
Protocol is chosen *for the test-fake exception*, not for production
ergonomics. The decision is correct; the rationale needs to catch up.

### ADR-003 (fully async) — accepted, applies cleanly

Cancellation propagation, thread-pool sizing, and sync/async mixing
are all correctly identified. The codebase follows it. No issue.

### ADR-004 (single LLM client) — implementation outdated

The decision still holds — "use a single class, switch providers at
the composition root, not inside the client." But the ADR's
implementation description is now **wrong**:

> Use a single `LLMClient` class that accepts a pre-configured async
> OpenAI client (`AsyncOpenAI` or `AsyncAzureOpenAI`) as a constructor
> argument.

The actual implementation is `LiteLLMClient` calling
`litellm.acompletion(model=...)` — which routes to OpenAI, Azure
OpenAI, Azure AI (Mistral), Anthropic, and many more by string-prefix
on the `model` parameter. LiteLLM *itself* is the "single client";
`LiteLLMClient` is a thin domain-aware wrapper around it.

This is actually a *stronger* application of the ADR's principle:
provider selection moved from "factory-time choice between
`AsyncOpenAI` and `AsyncAzureOpenAI`" to "runtime string-prefix on
`model`." But ADR-004 still describes the older shape, and Option B's
mitigation note ("the openai SDK maintainers have committed to
interface parity") is no longer the relevant guarantee — LiteLLM's
provider abstraction is.

**Recommendation:** amend ADR-004 to reflect LiteLLM. Mention that the
"add a third provider" Consequences clause was satisfied by switching
to LiteLLM (which natively handles the third+ providers) rather than
by adding a second adapter class.

### ADR-005 (Bearer auth) — accepted, applies as written

Matches `auth.validate_api_key` and the `HTTPBearer` security scheme
in `api.dependencies.authenticate_request`.

### ADR-006 (composed settings) — accepted, applies; one missing in arch.md

Sub-settings with `env_prefix` are in use. The settings layout has
since grown a `DatabaseSettings` group (env prefix `DB_`) for
PostgreSQL connection + `track_usage` flag — see migrate.py and
api/app.py. ADR-006 doesn't need to be amended (DB settings followed
the established pattern), but `architecture.md`'s settings section
still doesn't mention DB settings at all (see §2 below).

### ADR-007 (separate API schemas) — exemplary

The 2026-04-29 amendment is one of the strongest pieces of architectural
self-correction in this codebase. The original ADR said "always
separate API schemas from domain models." After observing it being
applied uniformly even where the domain object was already the right
external shape, the amendment narrows the rule to "separate only when
hiding fields, reshaping wire format, or adding HTTP-layer fields"
and otherwise prefers thin subclasses or returning the domain type
directly.

This is rare. Most "always do X" rules calcify when applied uniformly;
this one was relaxed when reality showed the rule was over-applied.
Worth preserving as a pattern: the codebase is willing to admit a rule
was over-fit and adjust without throwing it out entirely.

`★ Insight ─────────────────────────────────────`
- The pattern in ADR-007's amendment — "the rule still applies when
  these specific conditions hold; otherwise the rule's costs exceed its
  benefits" — is much more useful than either "always" or "never."
  It turns an architectural rule into a *checklist* that can be
  evaluated per-case. Future ADRs in this codebase should consider this
  shape: state the rule, state the conditions under which the rule's
  benefits actually accrue, and explicitly grant relief otherwise.
`─────────────────────────────────────────────────`

### ADR-008 (keep OrchestratorPort) — properly obsoleted

Marked superseded by ADR-011, lives in `obsolete/`. ✅

### ADR-009 / ADR-010 / ADR-012 — infrastructure ADRs, out of scope for this review

Skipping these — they cover Terraform state, container registry, and
PostgreSQL Entra admin setup. The first review didn't touch
infrastructure, and neither does this addendum.

### ADR-011 (drop OrchestratorPort) — accurate and well-reasoned

This is the ADR my first review most needed to read before
recommending "decompose the orchestrator into per-use-case services."
ADR-011 already addresses that path:

> Adding new use cases as methods on `StandardOrchestrator` (or
> extracting one into its own class when it grows enough to earn its
> own collaborators) is unaffected by this decision.

So the recommendation in §6.1 of the first review ("decompose
`Orchestrator`") is **not** in tension with ADR-011 — it's exactly the
escape valve ADR-011 anticipates: "extract when the use case is large
enough." The orchestrator is currently 723 lines with four unrelated
public methods; the `assign_codes` method alone is 160 lines of nested
loops with five private helpers. By any reasonable threshold,
`assign_codes` has earned its own class.

The first review's framing should have been "ADR-011 already gave you
permission to extract — it's just past time," not "decompose the
orchestrator." Update mentally: the recommendation is **anticipated by
ADR-011, not contrary to it**.

---

## 2. `docs/architecture.md` — significantly stale

This is the single biggest finding from this addendum. The
architecture document was clearly authored early in the project (when
it had 2 ports, 1 endpoint, and no DB) and has not kept pace with the
codebase. Concrete drift:

| Section | What architecture.md says | What the code does today |
|---|---|---|
| Package layout | `services/llm_client.py` for the LLM adapter | `adapters/llm_client.py` (the `services` directory contains application services only; LLM client moved to `adapters/`) |
| Layer diagram | 3 layers: API → Services → Domain | 4 packages: `api \| adapters` (siblings) → `services` → `domain`, per import-linter contract |
| Domain models | `FeedbackDocument` | `FeedbackItemModel` (renamed) |
| Domain models | `AnalysisResult` has fields `result, model, prompt_tokens, completion_tokens` | `AnalysisResultModel` has only `result: str`. The LLM-shaped fields moved to `LLMResponse` |
| Domain models | `LLMResponse` has field `text: str` | `LLMResponse[T_Response]` has `structured: T_Response` plus the LLM-shaped fields |
| Domain models | `TenantApiKey` has `key: str` | `TenantApiKey` has `key: SecretStr`, `key_id`, `is_superuser` |
| Ports | 2 ports (`LLMPort`, `OrchestratorPort`) | 3 ports (`LLMPort`, `AnonymizationPort`, `UsageRepositoryPort`); `OrchestratorPort` removed by ADR-011 |
| Concurrency model | "openai SDK's async client" | LiteLLM's `acompletion` |
| API contract | `/v1/analyze`, `/v1/health` | `/v1/analyze`, `/v1/summarize`, `/v1/summarize-aggregate`, `/v1/assign_codes`, `/v1/health`, `/v1/usage`, `/v1/usage/all` (and `/v1/usage` requires DB-backed tracking) |
| Settings | `LLMSettings`, `OrchestratorSettings`, `AuthSettings`, `LogSettings` | All four plus `DatabaseSettings` (env prefix `DB_`, with `track_usage`, `auth_mode`, `aad_scope`, etc.) |
| Statelessness | "No database, no disk cache, no request/response logging to file." | **No longer accurate.** Postgres-backed usage tracking is wired via `TrackingLLMAdapter` + `SqlAlchemyUsageRepository` when `DB_TRACK_USAGE=true` |
| Test directory | `tests/services/test_llm_client.py` | `tests/adapters/test_llm_client.py` (matches the package move) |
| Dependencies | `def get_orchestrator(request: Request) -> OrchestratorPort` | Returns `Orchestrator` (concrete) per ADR-011 |
| Composition root | "Domain has zero framework dependencies" — but the doc names Pydantic separately | ADR-001 explicitly chose Pydantic in domain; the architecture doc never updated to reflect that |
| Orchestrator logic | "Backoff: exponential with full jitter (base 1s, multiplier 2, cap 10s)" | Retry now lives in `LiteLLMClient` (`tenacity` retry on `LLMTimeoutError`/`LLMRateLimitError`); orchestrator does deadline math, not retry |

**Severity.** This isn't a "fix a typo" miss — `architecture.md` is the
top-level orientation document for new contributors. A new dev reading
it today will form a mental model that doesn't match the code. They'll
look for `services/llm_client.py` and not find it; they'll look for
`OrchestratorPort` and not find it; they'll assume statelessness and
not understand why there's a Postgres connection in `lifespan`.

**Recommendation.** Schedule a single rewrite pass on
`architecture.md`. Three options, in increasing order of effort:

1. **Stamp it as historical** with a top-of-doc banner and point
   readers at the current code. Lowest effort, but gives up the
   document.
2. **Bring it up to date in one pass.** Probably 2–3 hours: fix the
   table above, regenerate the layer diagram, add the new endpoints
   and the DB settings, replace the stateless statement with the
   conditional-on-DB_TRACK_USAGE story.
3. **Replace it with a generated reference + a short narrative
   intro.** Generate the package layout from `find src -type d`,
   generate the endpoint list from FastAPI's OpenAPI export, generate
   the settings table from Pydantic field introspection. Keep
   architecture.md as a 1–2 page narrative explaining the *why* (hex,
   composition root, anonymization story) and link out to the
   generated reference for *what*.

Option 2 is the realistic choice. Option 3 is the long-term win if
the team has the appetite — generated reference doesn't drift.

---

## 3. Test-fake bit-rot — two concrete bugs and two style issues

I ran `pytest tests/ --ignore=tests/e2e --ignore=tests/integration -x`
and got 190 passed in 7.93s. The codebase is *functionally* in good
shape. But the test fakes themselves have drifted from the real
contracts in ways that pydantic's permissive defaults are silently
covering up.

### Bug 1: `tests/e2e/conftest.py::FakeLLMPort.complete` has the wrong signature

**Current `LLMPort.complete` signature** (`src/qfa/domain/ports.py:26-33`):

```python
async def complete(
    self,
    system_message: str,
    user_message: str,
    tenant_id: str,
    response_model: type[T_Response],
    timeout: float = 20.0,
) -> LLMResponse[T_Response]: ...
```

**`FakeLLMPort.complete` in the e2e conftest** (lines 72-78):

```python
async def complete(
    self,
    system_message: str,
    user_message: str,
    timeout: float,
    tenant_id: str,
) -> LLMResponse:
```

Two problems:

1. `response_model` is missing entirely. The orchestrator passes it as
   a kwarg; this fake would `TypeError: unexpected keyword argument
   'response_model'`.
2. `timeout` and `tenant_id` are swapped relative to the port. Doesn't
   matter for kwarg calls, but it's a structural-typing mismatch and
   misleading to readers.

The e2e tests are gated by `pytest.mark.e2e` and excluded from the
default test run (`make db-up && make test-integration` is required).
So this bug has been silently waiting for the next e2e run.

### Bug 2: `queue_default_response` would raise on first call

`tests/e2e/conftest.py:54-70` defines:

```python
def queue_default_response(self, text: str = "ok", ...):
    self._queued.append(
        LLMResponse(
            text=text,                 # ← field doesn't exist
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )
    )
```

The current `LLMResponse` has `structured: T_Response` (required), no
`text` field. Calling `queue_default_response(...)` would raise a
`pydantic.ValidationError` because `structured` is missing.

**Verified that this is dead code.** Grep across `tests/` shows
`queue_default_response` is *defined* once and *called* zero times. So
it's never raised — but the next person who tries to use the helper
will be confused for a few minutes before noticing the field has been
renamed.

**Recommendation:** delete `queue_default_response`, or fix it to
`structured=AnalysisResultModel(result=text)` and document that
`structured` is now required.

### Style issue 1: `FakeOrchestrator` constructs domain models with stale fields

`tests/api/conftest.py:48-65`:

```python
self._analyze_result = analyze_result or AnalysisResultModel(
    result="Fake analysis result",
    model="gpt-4-test",          # ← no longer exists on AnalysisResultModel
    prompt_tokens=10,             # ← same
    completion_tokens=20,         # ← same
    cost=0.001,                   # ← same
)
```

The current `AnalysisResultModel` has only one field: `result: str`.
Pydantic v2's default `extra="ignore"` silently drops the four extra
fields. Tests pass (190/190), but the fixture is *misleading*: it
appears to be constructing a richer object than it actually is.

Same pattern in `tests/services/test_orchestrator.py::_make_llm_response`
and `_make_analysis_result` (lines 43-58 and 61-74).

**Recommendation:** strip the stale fields from the fixture
constructors. The "extra fields silently ignored" behavior is a
foot-gun: if `AnalysisResultModel` ever gets a `model_config =
ConfigDict(extra="forbid")` (which the project might want for
defense-in-depth), every test using these fixtures would suddenly fail
in ways that look unrelated to the change.

### Style issue 2: Three different `FakeLLMPort` classes

There are at least three independent `FakeLLMPort` implementations in
the test tree:

- `tests/e2e/conftest.py` — queue-based, with `calls` history
- `tests/services/test_orchestrator.py` — list-based with `responses`
  and `errors`, with `calls` history
- `tests/adapters/test_tracking_llm.py` — yet another shape

Each has a slightly different shape (different positional arguments,
different ways to seed responses, different ways to record calls).
None of them match the actual `LLMPort.complete` signature exactly.

This isn't necessarily wrong — different test contexts may need
different fake behaviors. But three near-duplicates with three
different signatures is one fewer copy than the rule of three says is
worth deduplicating. A shared `tests/_fakes/llm.py` with a single
queue-based `FakeLLMPort` whose signature is *exactly* the port would
cost ~30 lines and force the test fakes to update whenever the port
does (a CI win — the port stops being able to drift quietly).

---

## 4. API layer second pass

I read `api/dependencies.py`, `api/routes_usage.py`, `api/schemas.py`,
`api/schemas_usage.py`, and `cli/migrate.py` for this addendum.

### Finding: `ApiCodingNode` / `ApiCodingFramework` is an unfinished migration

`api/schemas.py` defines a fully typed coding-framework hierarchy:

```python
class ApiCodingNode(BaseModel):
    name: str
    children: list["ApiCodingNode"] = []
    @property
    def has_children(self) -> bool: ...
    def max_child_depth(self) -> int: ...
    def min_child_depth(self) -> int: ...

class ApiCodingFramework(BaseModel):
    root_codes: list[ApiCodingNode] = Field(min_length=1)
    @model_validator(mode="after")
    def verify_all_codes_have_same_depth(self) -> "ApiCodingFramework": ...
```

These models have **13 unit tests** in `tests/api/test_schemas.py`
covering `max_child_depth`, `min_child_depth`, equal-depth validation,
empty-roots rejection, etc. Solid work.

But `ApiAssignCodesRequest.coding_framework` (line 343) is still:

```python
coding_framework: dict[str, Any]
```

And the orchestrator still navigates the dict via `.get("types") or
[]` etc.

**This is an unfinished migration.** Someone built the typed model and
the tests, then never replaced the `dict[str, Any]` field. The typed
model is ready to wire in. Doing so is two steps:

1. Replace `coding_framework: dict[str, Any]` with `coding_framework:
   ApiCodingFramework` in `ApiAssignCodesRequest`.
2. Add a corresponding domain model
   `qfa.domain.models.CodingFramework` (it'll be near-identical to
   `ApiCodingFramework`, minus the `model_validator` if you want it to
   stay structural-only at the domain layer) and update
   `CodingAssignmentRequestModel.coding_framework` to use it.
3. Update the orchestrator's `assign_codes` to navigate the typed
   structure instead of `.get(...)` chains.

**The "third strongest single architectural smell" from the first
review was already half-fixed — just not connected.** A few hours of
plumbing work cleans up the bug entirely, and the existing 13 tests
already prove the structural model works. The wiring is the only
missing step.

### Finding: API field-name inconsistency between endpoints

| Endpoint | Field | Field name |
|---|---|---|
| `POST /v1/analyze` | feedback container | `documents` |
| `POST /v1/analyze` | per-item body | `text` |
| `POST /v1/summarize` | feedback container | `feedback_items` |
| `POST /v1/summarize` | per-item body | `content` |
| `POST /v1/summarize-aggregate` | (same body shape as summarize) | `feedback_items` / `content` |
| `POST /v1/assign_codes` | feedback container | `feedback_items` |
| `POST /v1/assign_codes` | per-item body | `content` |

The same conceptual entity — a feedback record submitted by a
community member — has two different containers (`documents` vs.
`feedback_items`) and two different per-item body fields (`text` vs.
`content`).

This is a real consumer-facing wart. A CRM client integrating with the
API has to remember which endpoint uses which field name. The
divergence likely traces back to `/v1/analyze` being the original
endpoint (before the ubiquitous-language work landed) and the others
being added after.

**Recommendation:** harmonize on one shape. Per the UL doc, the
preferred terms are "feedback record" and "feedback description," so:

- Container: `feedback_items` (current; matches three of four
  endpoints)
- Body: `content` (current; matches three of four endpoints)

That makes `/v1/analyze` the outlier. Migration: deprecate
`documents`/`text`, accept either via Pydantic field aliases for one
release, then drop the old names.

### Finding: `routes_usage` validates time windows in the route, not the schema

`routes_usage._parse_time_window` (lines 60-89) walks `from_`/`to`
checking timezone-awareness and ordering, raising `HTTPException(422,
{...})` directly. Could equally well be done as a Pydantic dependency
or a `model_validator` on a query-params model.

This is a minor stylistic point, not a bug. The current placement does
the job, and FastAPI's `Query(default=None, alias="from")` already
handles the alias and parsing. I'd leave it alone unless the same
window pattern shows up on a third endpoint, at which point extracting
to a shared dependency makes sense.

### Finding: `cli/migrate.py` is genuinely well-designed

Multi-replica safety via Postgres advisory lock
(`pg_advisory_lock(LOCK_KEY)`), session-scoped so a crashed migrator
auto-releases. Run from `entrypoint.sh` before uvicorn binds, not from
the lifespan — so migrations don't race the app's startup. The
docstring explicitly explains the trade-off ("the lock is
session-scoped: it is released automatically when the holding
connection closes, so a crashed migrator cannot leave the keyspace
permanently held").

**Positive callout.** This is the kind of detail that turns up only
when a senior person has thought about the operational story. The
first review missed it because I didn't open this file.

### Finding: `api.dependencies.get_orchestrator -> Orchestrator` confirms ADR-011

```python
def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator
```

Concrete return type, not `OrchestratorPort`. Matches ADR-011's
prescription.

### Finding: `get_usage_repo` cleanly distinguishes "feature off" from "DB down"

```python
def get_usage_repo(request: Request) -> UsageRepositoryPort:
    repo = getattr(request.app.state, "usage_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "usage_tracking_disabled",
                "message": "Usage tracking is not enabled",
            },
        )
    return repo
```

This pairs with `UsageRepositoryUnavailableError → 503
{"code": "usage_backend_unavailable"}` in `api/app.py`. So the API
returns two distinct 503 codes:

- `usage_tracking_disabled` — feature flag is off, never going to
  succeed without redeploy.
- `usage_backend_unavailable` — feature is on, DB is transiently down,
  retry/backoff is appropriate.

That's a *thoughtful* error design — most APIs collapse these into one
opaque 503 and force the consumer to guess. ✅

---

## 5. Ubiquitous Language drift

The `docs/ubiquitous_language.md` document declares a careful,
IFRC-aligned vocabulary:

- **Use:** *feedback record*, *feedback description*, *community
  member*, *coding framework*, *insight*
- **Avoid:** *document*, *case*, *ticket*, *entry*, *item*, *user*
  (for community members), *beneficiary*, *message*, *transcript*

The codebase has not consistently adopted it. Concrete drift:

| Place | What it uses | UL says |
|---|---|---|
| Domain model `FeedbackItemModel` | "Item" | Avoid "item" — use "feedback record" |
| `ApiAnalyzeRequest.documents` | "documents" | Avoid "document" |
| `ApiAnalyzeRequest.documents[].text` | OK ("text" is neutral) | Acceptable |
| Orchestrator `_SYSTEM_MESSAGE_TEMPLATE` | "humanitarian organisation" | OK |
| Orchestrator `_SYSTEM_MESSAGE_TEMPLATE` | "beneficiary feedback data" | **Avoid "beneficiary"** |
| Orchestrator `_DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT` | "beneficiary feedback items" | Same |
| `coding_classifier._JUDGE_SYSTEM` | "beneficiary feedback" | Same |
| `tests/api/test_schemas.py` example coding levels | "Water > Distribution > Waiting times" | Fine — these are example codes, not vocabulary |
| `ApiSummarizeRequest` example prompt | "operational issues and beneficiary experience" | **Avoid "beneficiary"** |

The drift falls into three buckets:

1. **API field names use forbidden terms** (`documents`, plus
   `FeedbackItemModel` over `FeedbackRecord`). External-facing — visible
   to every API consumer and every developer reading the OpenAPI spec.
2. **LLM system prompts use forbidden terms** ("beneficiary feedback"
   appears at least four times in the orchestrator and classifier).
   This actually matters — the LLM's outputs will mirror the language
   it's prompted with, so saying "beneficiary" in the system prompt
   makes it more likely to appear in `analysis`/`summary` text returned
   to the consumer.
3. **Documentation and Swagger examples** (the `ApiSummarizeRequest`
   example prompt). Visible in the API documentation that consumers
   read first.

For a project where the IFRC vocabulary is part of the *product*
(qualitative feedback analysis for humanitarian work, where "community
member vs beneficiary" is a meaningful distinction in the domain), the
drift isn't cosmetic — it's a small but real misalignment with the
project's stated identity.

**Recommendation, prioritized:**

1. **System prompts** — replace "beneficiary feedback" with "community
   feedback" or "feedback records from community members" in
   `orchestrator.py` and `coding_classifier.py`. This affects model
   outputs and is the highest-leverage change.
2. **API field naming** — harmonize per §4 above (drop `documents`,
   keep `feedback_items` everywhere). Same release as the field-name
   harmonization.
3. **Domain model name** — rename `FeedbackItemModel` →
   `FeedbackRecordModel` (or just `FeedbackRecord`). Largest blast
   radius, lowest user-visible impact. Bundle with #2.
4. **Swagger examples** — easiest to fix, lowest priority, do
   alongside any of the above.

---

## 6. Production composition + e2e wiring — positive callouts

These are things the codebase does *well* that the first review didn't
credit because I hadn't read these files.

### E2E tests boot the real composition root

`tests/e2e/conftest.py::e2e_app` uses `LifespanManager(app)` with
`create_app(llm_factory=lambda _settings: fake_llm)`. That means:

- The **real** `lifespan` runs.
- The **real** `TrackingLLMAdapter` wraps the fake LLM (because the
  lifespan does that wrap based on `DB_TRACK_USAGE=true`).
- The **real** `PresidioAnonymizer` is constructed.
- The **real** Postgres-backed `SqlAlchemyUsageRepository` is wired.

Only the bottom-most layer (the LLM call) is faked. Everything above
it — middleware, routing, exception handlers, dependencies, lifespan
ordering, DB engine creation, AAD-token connection auth (in entra
mode) — runs as in production.

This is a **canonical hex test setup**: the seam is the port, the
adapter swap happens at the composition root, and no monkeypatching
is needed. The first review missed this because it didn't open the
e2e conftest. Worth documenting as a pattern other projects could copy.

### Migration uses a Postgres advisory lock

Already discussed in §4. Multi-replica-safe migration is a problem
many projects discover only when their second pod starts up
mid-migration and breaks. This codebase solved it deliberately, with
session-scoped lock release as a crash-safety property. ✅

### The `auth_mode = "entra"` path

`adapters/db.py` injects fresh AAD access tokens via SQLAlchemy's
`do_connect` event hook (`_AadTokenProvider` caches, refreshes 120s
before expiry). This is a small but production-grade integration — the
kind of thing that's frequently bolted on at deploy time and breaks in
ways that are hard to diagnose. Doing it in the adapter, with cache
semantics explicit, is the right shape.

### Two-tier API tests

`tests/api/conftest.py` builds a `FastAPI` instance directly (no
lifespan), because route-level tests don't need the real LLM, real DB,
or real Presidio. `tests/e2e/conftest.py` boots `create_app` with
`LifespanManager`, because end-to-end behavior tests *do* need
production composition. Two tiers, each fit for purpose, ~155 lines
each. Clean separation.

---

## Updated action list (supersedes §"Recommended action list" in the original review)

I'm marking each item with the source review (R1 = original, R2 = this
addendum) and adjusting priorities based on what the addendum found.

| # | Action | Effort | Source | Notes |
|---|---|---|---|---|
| 1 | Delete dead `_INJECTION_PATTERNS` from `orchestrator.py:120-124` | 5 min | R1 | Still applies |
| 2 | Drop the stale `tenacity` TODO in `pyproject.toml`; tighten contract | 5 min | R1 | Still applies |
| 3 | Move prompt-injection check + token-limit check out of `LiteLLMClient`; pick one layer for each | 15 min | R1 | Still applies |
| 4 | Fix `FakeLLMPort.complete` signature in `tests/e2e/conftest.py` to match the real port | 10 min | R2 | New — concrete bug |
| 5 | Delete or fix `queue_default_response` (dead code that would raise on first use) | 5 min | R2 | New — concrete bug |
| 6 | Strip stale fields from `FakeOrchestrator` and `_make_llm_response`/`_make_analysis_result` | 20 min | R2 | Reduces foot-gun if `extra="forbid"` ever lands |
| 7 | Move `services/call_context.py` to a layer that adapters can read from cleanly | 30 min | R1 | Still applies |
| 8 | **Wire in the existing `ApiCodingFramework` typed model** to replace `coding_framework: dict[str, Any]` | 1–2 hours | R2 (replaces R1's "build a typed model") | Half-done already; the model + tests exist. Just plumb it through. |
| 9 | Update `ADR-002` to capture the production-inheritance / test-fake-structural-typing pattern that `AGENTS.md` already documents | 30 min | R2 | Documentation hygiene |
| 10 | Update `ADR-004` to reflect LiteLLM (decision still holds; implementation description outdated) | 30 min | R2 | Documentation hygiene |
| 11 | **Rewrite (or stamp as historical) `docs/architecture.md`** | 2–3 hours | R2 | Highest doc-debt item |
| 12 | Harmonize API field names: `documents`/`text` on `/v1/analyze` → `feedback_items`/`content` (with Pydantic aliases for one release) | ½ day | R2 | Consumer-facing |
| 13 | Replace "beneficiary" in LLM system prompts with UL-aligned vocabulary | 30 min | R2 | Highest UL-leverage change — affects model outputs |
| 14 | Decompose `Orchestrator` into per-use-case services + push prompt templates and `_ScoredCode` into `qfa.domain` | ½–1 day | R1 | ADR-011 explicitly anticipates this |
| 15 | Strategic decision on `LLMPort` altitude (Option A/B/C); document outcome as ADR-013 | strategic | R1 | Unchanged — still a strategic call |

Items 1–10 are all under half a day combined and remove every
non-strategic smell from both reviews. Item 11 is the largest single
chunk of work and should probably happen alongside item 14 since
that's when the package layout would shift again.

---

## What this addendum *does* claim

After reading the ADRs, the skipped API files, and the test fixtures,
and running the test suite, I'm now confident in:

- The original review's six answers, with the corrections in §1
  (ADR-001 already justified the Pydantic-in-domain choice; ADR-011
  already anticipates orchestrator decomposition; ADR-002 has its own
  internal contradiction).
- The two test-fake bugs in §3 — verified by reading the code and
  confirmed dead/silently-absorbed by running the tests.
- The `architecture.md` drift in §2 — verified line-by-line against
  `src/qfa/`.
- The unfinished `ApiCodingFramework` migration in §4 — verified the
  typed model exists, has tests, and isn't wired in.
- The ubiquitous-language drift in §5 — verified by grep against the
  UL doc's avoid-list.

## What this addendum still does *not* claim

- I did not run `tests/e2e` or `tests/integration` (require Postgres
  via `make db-up`). The two e2e fake bugs (§3, items 1 and 2) are
  inferred from reading the code; running the suite would confirm or
  rebut.
- I did not look at `tests/integration/test_db_postgres.py` or
  `tests/integration/test_migrations.py` — these likely contain
  important detail about the DB adapter behavior, but they were out
  of scope for the addendum's core question.
- I did not read the auxiliary fixtures (`tests/conftest.py`,
  `tests/api/test_schemas.py` beyond what grep showed,
  `tests/test_settings.py`).
- I did not read the infrastructure ADRs (009, 010, 012) since they
  are out of scope for code-architecture review.
- I did not look at the actual COVID-19 coding framework in
  `fixtures/coding_framework.json` — the JSON file the API examples
  load from. If the fixture's structure differs from what
  `ApiCodingFramework` expects, that would block item 8 above.
