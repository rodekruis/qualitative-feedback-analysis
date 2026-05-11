# Architecture Review — 2026-05-08

**Reviewer:** Claude (Opus 4.7, conversational review)
**Scope:** answers six concrete architectural questions raised in the
2026-05-08 conversation about whether the hex layout is paying for itself,
where it leaks, and what a green-field redo would look like.
**Method:** read the full `qfa.domain`, `qfa.services`, `qfa.adapters`,
and the FastAPI composition root in `qfa.api.app`; cross-checked against
`pyproject.toml`'s `import-linter` contracts; counted call sites with
`grep`. No tests were run. Worktree:
`/workspace/.claude/worktrees/arch-review-2026-05-08/`.

---

## TL;DR

| Question | Verdict |
|---|---|
| 1. Are ports domain-shaped or adapter-shaped? | **Mixed.** `AnonymizationPort` and `UsageRepositoryPort` are clean and domain-shaped. `LLMPort` is **adapter-shaped** — its surface mirrors a chat-completions API, and `LLMResponse` exposes `prompt_tokens`/`completion_tokens`/`cost`/`model` directly. This is a deliberate "provider-portability" port, not a "do an analysis" port. Whether that's the right level depends on what you actually want to swap. |
| 2. Cross-layer imports? | **Domain is clean.** Services is clean. **One real smell:** `qfa.adapters.tracking_llm` imports `qfa.services.call_context`, which is an adapter→services dependency that the layered import-linter contract permits (`api \| adapters` are sibling outer layers, both can see `services`) but which signals that `call_context` is misplaced — see Q4. |
| 3. `Any`-typed leakage on ports/seams? | **One material case:** `CodingAssignmentRequestModel.coding_framework: dict[str, Any]`. The orchestrator navigates this blob by `.get("types")`, `.get("categories")`, `.get("codes")`, `str(...)`-coercing every value. The type system is doing nothing here; the API schema validates the outer shape and then the typing collapses. |
| 4. De-facto ports (seams without a `Protocol`)? | **Three.** `services.call_context` is a `ContextVar`-based seam between orchestrator and tracking adapter — it's the missing port. `auth.validate_api_key` is a free function used as auth seam. `LLMFactory = Callable[[LLMSettings], LLMPort]` in the composition root is an explicit DI hook, not really a smell. |
| 5. Per-port call-site density? | **All ports earn their keep.** `LLMPort` has 3 implementations (LiteLLM, Tracking decorator, FakeLLMPort) and is called from 5 call sites in the orchestrator. `AnonymizationPort` has 1 implementation but 5 call sites. `UsageRepositoryPort` has 1 implementation and is consumed by both write (TrackingLLMAdapter) and read (`routes_usage`) paths. None are "single-impl, single-caller" overkill. |
| 6. Is hex the right architecture? | **Yes — keep hex, but reshape.** The project has the multi-adapter shape that hex earns its keep on (LLM provider + Presidio + Postgres + Azure auth + sensitive-data audit story). The cost is bounded (~300 lines of port + composition glue). What's wrong is *internal layering*: the orchestrator is a 723-line god-class, the domain layer is anemic, and one port (`LLMPort`) is at the wrong altitude relative to the domain. |

---

## Q1 — Are the ports domain-shaped or adapter-shaped?

This is the most consequential question of the six. The answer differs per port.

### `AnonymizationPort` — domain-shaped ✅

```python
def anonymize(self, text: str) -> tuple[str, dict[str, str]]: ...
def deanonymize(self, text: str, mapping: dict[str, str]) -> str: ...
```

The surface is "redact PII, then later restore it." It says nothing about
spaCy, recognizers, or operator configs. A rule-based replacer or a
hand-coded fake satisfies it identically. The contract describes what the
domain wants (PII out, then PII back in for the response), and the
adapter layer absorbs all the Presidio-specific machinery. **Verdict:
clean port.**

### `UsageRepositoryPort` — domain-shaped ✅

```python
async def record_call(self, record: LLMCallRecord) -> None: ...
async def get_usage_stats(self, tenant_id: str, ...) -> UsageStats: ...
async def get_all_usage_stats(self, ...) -> list[UsageStats]: ...
```

`LLMCallRecord`, `UsageStats`, `DistributionStats`, `TokenStats` are all
domain models. The port doesn't expose SQL, sessions, or transactions —
those stay inside `SqlAlchemyUsageRepository`. The only mild leak is the
`UsageRepositoryUnavailableError`, which is a domain error that
specifically describes a transient-DB symptom, but that's acceptable —
the API needs to distinguish "feature off" from "DB down" to drive
retry/backoff. **Verdict: clean port.**

### `LLMPort` — adapter-shaped ⚠️

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

This is **the chat-completions API verbs**: system message, user message,
structured response, timeout. And `LLMResponse[T_Response]` exposes
`model`, `prompt_tokens`, `completion_tokens`, `cost` — five fields that
are LLM-shaped, not domain-shaped (the domain doesn't *care* about
prompt_tokens; the tracking adapter cares).

This is a **provider-abstraction port**, not a domain-operation port. It
makes one specific question cheap to answer: "can we swap LiteLLM for a
hand-rolled OpenAI client, or for a fake?" Yes, and you do — there are
three implementations (`LiteLLMClient`, `TrackingLLMAdapter`, the
`FakeLLMPort` in tests).

What it does *not* abstract is the question "could this analysis be done
by something other than an LLM?" — for that, you'd want a domain-shaped
port like:

```python
class AnalysisPort(Protocol):
    async def analyze(
        self,
        documents: tuple[FeedbackItemModel, ...],
        instruction: str,
        tenant_id: str,
        deadline: datetime,
    ) -> AnalysisResultModel: ...
```

The cost of choosing the current "low-altitude" shape is that all the
prompt assembly, the deadline-vs-timeout math, the retry policy, the
judge orchestration, and the anonymize-call-deanonymize dance live in
`Orchestrator`. That's why `Orchestrator` is **723 lines**. A
domain-shaped port would push prompt construction into the adapter,
where it arguably belongs (each LLM provider has its own quirks).

**Verdict on the LLMPort shape:** defensible but worth a deliberate
re-decision. The current shape is right *if* you expect to swap
providers more often than you expect to swap analysis strategies. For a
humanitarian-feedback project with sensitive data and a possible future
need for hybrid (LLM + rule-based + human-in-the-loop) workflows, I'd
lean toward a domain-shaped port. See Q6 for the green-field
recommendation.

### Aside: `T_Response` bounds

`T_Response = TypeVar("T_Response", bound=Union[BaseModel, str])` couples
the domain port to Pydantic's `BaseModel`. This is a soft architectural
leak — Pydantic is technically infrastructure, but it's used so
pervasively (every domain model is a `BaseModel`) that calling it a
violation would be pedantic. `import-linter` correctly does **not**
forbid `pydantic` in the domain; the line gets drawn at provider SDKs
(`openai`, `litellm`, `presidio_*`, `fastapi`, `starlette`, `tenacity`).
That's a reasonable place to draw it.

---

## Q2 — Cross-layer import audit

`import-linter` runs in CI and is configured with two contracts (see
`pyproject.toml`):

1. **Layered:** `api | adapters` are siblings at the outer ring;
   `services` is the middle layer; `domain` is the innermost. `api` and
   `adapters` cannot import each other except via four explicit
   allowlist entries for `qfa.api.app` (the composition root).
2. **Forbidden modules:** `qfa.domain` cannot import `openai`,
   `litellm`, `presidio_*`, `fastapi`, `starlette`, or `tenacity`.
   `qfa.services` cannot import the same set minus `tenacity`.

Grep confirms the rules hold:

| Check | Result |
|---|---|
| `qfa.domain` imports from `adapters` / `services` / `api` / `settings` | **None.** ✅ |
| `qfa.services` imports from `adapters` / `api` | **None.** ✅ |
| `qfa.adapters` imports from `services` | **One:** `adapters.tracking_llm` imports `services.call_context.current_call_context` (line 19). |

The single adapter→services import is technically permitted by the
import-linter layered contract (services sits *below* adapters, so
adapters reading from services is fine — the arrow points the right
way). But it's a *signal* that `call_context` is in the wrong place. See
Q4.

### Stale TODO in import-linter contracts

`pyproject.toml` has a TODO comment claiming `tenacity` is still
imported in `services.orchestrator` for retry decoration:

```toml
# TODO: tenacity is still imported in qfa.services.orchestrator for retry
# decoration. Once retry policy moves behind a port (or to a wrapper in
# qfa.adapters), drop "tenacity" from allow_indirect_imports below and
# add it to forbidden_modules above.
```

This is **stale** — `grep -rn "tenacity\|@retry" src/qfa/services` finds
zero hits. Retry logic now lives in `LiteLLMClient` (request-level retry
on `LLMTimeoutError`/`LLMRateLimitError`) and in `TrackingLLMAdapter`
(record-write retry on connection-class errors). The orchestrator does
deadline math but no retries.

**Action:** drop the TODO and tighten the contract by adding `tenacity`
to `forbidden_modules` for `qfa.services`. This costs nothing and closes
a door that's already closed in code.

---

## Q3 — `Any`-typed leakage on ports / seams

Searching `Any` in `domain/ports.py` and `domain/models.py`:

- `domain/ports.py`: zero hits. ✅
- `domain/models.py`: one hit, but it matters —
  ```python
  class CodingAssignmentRequestModel(BaseModel):
      ...
      coding_framework: dict[str, Any] = Field(
          description="Hierarchical coding framework with types, categories, and codes.",
      )
  ```

The orchestrator then navigates this blob with:

```python
types = request.coding_framework.get("types") or []
type_entry = types[type_index]
type_name = str(type_entry.get("name", ""))
categories = type_entry.get("categories") or []
codes = category.get("codes") or []
code_id = str(code.get("code_id", ""))
```

Six distinct `.get()` traversals with default-empty-list and
`str(...)`-coerced fields. The type system literally cannot help with
this. If a caller submits a coding framework with `categories` named
`subcategories` instead, the LLM gets called once with an empty options
list and the bug surfaces only at runtime as zero results.

This is the **strongest single architectural smell** in the codebase.
The fix is straightforward — define the structure in domain:

```python
class CodingFrameworkCode(BaseModel):
    code_id: str
    name: str

class CodingFrameworkCategory(BaseModel):
    name: str
    codes: tuple[CodingFrameworkCode, ...] = ()

class CodingFrameworkType(BaseModel):
    name: str
    categories: tuple[CodingFrameworkCategory, ...] = ()

class CodingFramework(BaseModel):
    types: tuple[CodingFrameworkType, ...]
```

Pydantic will validate the structure at the API boundary, the
orchestrator stops needing defensive `.get()` chains, and the tests
gain typed factories. This is ~30 lines added and ~20 lines of
defensive `.get(...) or []` removed.

---

## Q4 — De-facto ports (seams without a `Protocol`)

A "de-facto port" is anywhere two layers communicate through a contract
that *isn't declared as a port*. Three candidates:

### `services.call_context` — the missing port (real smell)

```python
# qfa.services.call_context
current_call_context: ContextVar[CallContext | None] = ContextVar(...)

@asynccontextmanager
async def call_scope(tenant_id: str, operation: Operation) -> ...
```

The orchestrator *writes* the context (`async with call_scope(...)` in
all four public methods). `TrackingLLMAdapter` *reads* it
(`current_call_context.get()` at line 66). This is a one-way channel
from services to an adapter — exactly what a port describes — but it's
expressed as a module-level `ContextVar`. Consequences:

1. **Adapter→services import.** `tracking_llm` imports from
   `services.call_context`. The layered contract permits it, but
   the dependency points "up" against the natural hex flow.
2. **Implicit contract.** Forgetting `call_scope(...)` means
   `MissingCallScopeError` at runtime. There's no type-system signal at
   the port boundary that the LLM call requires a context.
3. **Singleton coupling.** The `ContextVar` is module-global; tests
   that try to swap behavior must reset it, and the docstring on
   `call_scope` warns about asyncio task propagation.

**Cleaner placement.** `CallContext` is already a domain model, and
`call_scope` is pure (no infra). Move `call_context.py` to `qfa.domain`
(or to a thin `qfa.services.context` that imports nothing from infra,
which is what we have, just relabel the layer). Either way, `tracking_llm`
should import the ContextVar from a layer at or below it, not from a
peer service module. This is a low-risk refactor: ~5 lines of imports
move.

### `auth.validate_api_key` — a function-as-port (mild smell)

```python
def validate_api_key(provided_key: str, api_keys: list[TenantApiKey]) -> TenantApiKey: ...
```

This is the auth boundary expressed as a free function. `qfa.api.app`
calls it directly in middleware, and `qfa.api.dependencies`
(presumably) calls it from FastAPI dependencies. There's no
`AuthenticationPort`. For now this is fine — there's exactly one auth
strategy (constant-time-comparison API key match) and one place it's
used. Promoting it to a port would be premature. Worth flagging if a
second auth strategy ever lands (OAuth, mTLS, etc.) so this gets
formalized rather than grown another `validate_oauth_token` function
beside it.

### `LLMFactory = Callable[[LLMSettings], LLMPort]` — DI hook, not a smell

```python
LLMFactory = Callable[[LLMSettings], LLMPort]
```

Used in `create_app(llm_factory=...)` so e2e tests can inject a fake
without monkeypatching `qfa.adapters.llm_client.LiteLLMClient`. This is
a **clean** DI seam — explicit, typed, and lives in the composition
root where it belongs. No issue.

### `services.coding_classifier` — internal helpers, not a port

`build_pick_messages`, `build_judge_messages`, `parse_selected_indices`
are pure functions used by the orchestrator. They have no external impl
to swap. Internal helpers, not seams. No issue.

---

## Q5 — Call-site density per port

Counted via `grep`:

| Port | Production impls | Test fakes | Call sites in services | Call sites in api / other |
|---|---|---|---|---|
| `LLMPort` | 2 (`LiteLLMClient`, `TrackingLLMAdapter`) | 1 (`FakeLLMPort` in `tests/e2e/conftest.py`, plus inline fakes in `tests/services/test_orchestrator.py` and `tests/adapters/test_tracking_llm.py`) | 5 (`analyze`, `summarize`, `summarize_aggregate`, `_pick_code_indices`, `_judge_code_level` — all `await self._llm.complete(...)`) | 0 |
| `AnonymizationPort` | 1 (`PresidioAnonymizer`) | inline fakes in tests | 5 `.anonymize` + 3 `.deanonymize` calls in orchestrator | 0 |
| `UsageRepositoryPort` | 1 (`SqlAlchemyUsageRepository`) | inline fake in `tests/adapters/test_tracking_llm.py` | 1 (`tracking_llm._record_with_retry → self._usage_repo.record_call`) | 2 in `api/routes_usage.py` (read paths via FastAPI `Depends`) |

**Reading the table:**

- No port is "single-impl, single-caller" overkill. Each is consumed
  from at least 2 distinct call sites or layers.
- `LLMPort` is the most decorated (production wraps `TrackingLLMAdapter
  → LiteLLMClient`) — a real beneficiary of the port pattern, since the
  decorator only works because there's a stable interface to decorate.
- `UsageRepositoryPort` carries its weight by being consumed from
  *both* sides: the write-side decorator (`TrackingLLMAdapter`) and the
  API read-side (`routes_usage.list_usage_stats` via `Depends`). Two
  callers in two layers.

**No "remove this port" recommendations from call-site density alone.**

---

## Q6 — Is hexagonal the right architecture?

**Greenfield recommendation: yes, keep hexagonal — and reshape three
things internally.**

### Why hex earns its keep here

The honest YAGNI question is *"how many distinct external worlds does
the inner core talk to?"* For this project:

1. **LLM provider** (LiteLLM, which itself fans out to OpenAI/Azure
   OpenAI/Azure AI/Anthropic/etc.) — talked to in 5 call sites.
2. **PII detection** (Presidio + spaCy) — talked to in 5 call sites.
3. **Usage persistence** (Postgres via SQLAlchemy + asyncpg, with
   AAD/Entra-token connection auth) — talked to in 1 write + 2 read
   sites.
4. **Pricing data** (LiteLLM cost map + custom YAML) — startup-time only.

Four genuinely distinct external concerns is comfortably above the bar
where hex stops being overkill. Layered architecture (Controller →
Service → Repository) handles 1–2 external concerns well; hex starts
earning its keep around 3+, especially when the concerns change at
different rates and ship from different vendors.

The **sensitive-data + audit story** doubles the case. Humanitarian
beneficiary feedback under GDPR/ICRC-style constraints means you need a
defensible answer to "is the domain layer free of vendor SDKs?" — and
`import-linter` gives you that answer in CI. Layered architecture
doesn't enforce that; hex with `import-linter` does.

### Why the LoC tax isn't actually paying for hex — it's paying for *features*

Inventory of "hex tax" in this codebase:

| Component | LoC | Marginal cost vs. flat layout |
|---|---|---|
| `qfa.domain.ports` | 159 | 159 (this code only exists because of hex) |
| `qfa.domain.models` (request/response/result models) | ~250 | 0 — you'd write these models in any architecture |
| `qfa.domain.models` (LLMCallRecord, UsageStats, etc.) | ~150 | ~50 (hex pushes these to the inner layer instead of inlining in the repo class) |
| `qfa.domain.errors` | 86 | ~30 (errors get richer when they're a stable domain contract) |
| `qfa.api.app` lifespan composition | ~80 | ~80 (the composition root is hex's signature glue code) |
| `import-linter` contracts | ~60 | 60 |

**Total marginal hex cost: ~380 lines of Python + 60 lines of TOML.**
Out of 6,129 implementation lines. That's ~6% overhead, not the 50%
overhead a casual look at the file count might suggest. The rest of the
LoC is the actual work the service does.

### What a green-field redo would change

Three concrete reshapes, in order of importance:

#### 1. Decompose `Orchestrator` into per-use-case services

`Orchestrator` is currently a 723-line class with four unrelated public
methods (`analyze`, `summarize`, `summarize_aggregate`, `assign_codes`)
that share a constructor but not much else. ADR-011 explicitly chose
this single-service pattern over per-task orchestrators, but at this
size it's reading like a god-class:

- `analyze` and `summarize` share ~70% of their structure (anonymize →
  call LLM with one message → deanonymize). Could collapse into one
  `_run_single_completion(...)` helper plus per-method prompt config.
- `assign_codes` is 160 lines of nested for-loops over hierarchy
  levels — a clear candidate to extract into `CodingService` with its
  own `_pick_indices` and `_judge_level` helpers as private methods.
- `summarize_aggregate` runs *two* LLM calls (summary + judge) inline
  with no abstraction; the judge step looks like it wants to be its own
  small service or strategy.

This is not a hex critique — it's a within-services design critique.
Hex tells you "services depend only on ports"; it does not tell you "one
service class with N methods" vs. "N service classes with one method."
Greenfield, I'd write `AnalyzeService`, `SummarizeService`,
`AggregateSummarizeService`, `CodingService` — each ~150 lines, each
testable in isolation, each with its own constructor surface (some need
both LLM and anonymizer; the judge could be its own injected
collaborator).

#### 2. Reconsider the `LLMPort` altitude

As discussed in Q1, the current port is provider-shaped, which forces
the orchestrator to know about prompt assembly. Greenfield, weigh:

- **Option A (current shape, "low-altitude port"):** keep `LLMPort.complete(messages, ...)`.
  Cheap to swap providers; orchestrator owns prompts and retries.
  Good if the future is "more LLM providers" or "different LLM
  providers per tenant."

- **Option B (high-altitude port, "domain-shaped"):**
  ```python
  class AnalysisPort(Protocol):
      async def analyze(
          self,
          documents: tuple[FeedbackItem, ...],
          instruction: str,
          tenant_id: str,
          deadline: datetime,
      ) -> AnalysisResult: ...

  class SummarizationPort(Protocol): ...
  class CodingPort(Protocol): ...
  ```
  Adapter classes implement each port via LLM (with prompts inside the
  adapter). Good if the future is "swap LLM-based for rule-based or
  human-in-the-loop." Costs more ports but shrinks the orchestrator
  dramatically.

- **Option C (hybrid):** a low-altitude `LLMCompletionPort` *and* a few
  high-altitude domain ports that internally use it. Best of both, more
  surface area to maintain.

For this project (humanitarian, sensitive data, possible regulatory
push toward auditable rule-based fallbacks), I'd lean **Option B**.
But this is a strategic call that depends on what the team thinks is
more likely to change — and either is defensible hex.

#### 3. Type the coding framework, type-hide the call context

These are the two structural smells from Q3 and Q4:

- Replace `coding_framework: dict[str, Any]` with a typed
  `CodingFramework` model in `qfa.domain.models` (~30 lines).
- Move `call_context.py` from `qfa.services` into `qfa.domain` (or
  rename to express that it's a cross-cutting domain seam, not a
  service). Adapters then import the ContextVar from a strictly lower
  layer, fixing the one inverted import.

### Why not pick a simpler architecture?

For completeness, here's why the alternatives don't fit:

- **Layered (Controller → Service → Repository):** would handle the
  Postgres path fine, but doesn't naturally express the LLM and
  anonymizer adapters or the decorator stack
  (`TrackingLLMAdapter`-wraps-`LiteLLMClient`). You'd end up with hex
  patterns informally implemented without the contracts to enforce
  them.
- **"Pragmatic" / no formal architecture:** save ~380 lines, lose the
  layer enforcement, lose the test seams (the e2e tests rely on
  `create_app(llm_factory=fake)` which only works because `LLMPort` is
  a Protocol with a clean shape), and lose the audit story. In a
  humanitarian-data context, the audit story alone justifies the cost.
- **DDD-heavy with aggregates and event sourcing:** out of scope for
  the problem. The domain is mostly request/response with some
  hierarchical traversal; you don't have aggregates with invariants or
  long-lived entity identities.

**Greenfield: hex stays. The internal shape inside `services` is what
needs work.**

---

## Bonus findings (smells that fell out during reading)

These weren't asked about but are worth flagging while the context is
fresh.

### Dead `_INJECTION_PATTERNS` block in orchestrator (real bug)

`src/qfa/services/orchestrator.py:120-124` declares
`_INJECTION_PATTERNS` at module level — but **nothing in the
orchestrator uses it.** The actual prompt-injection check happens
inside `LiteLLMClient._check_injection`, which has its own copy of the
same patterns at function scope.

```python
# orchestrator.py:120 — DEAD CODE
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("role_prefix", re.compile(r"^\s*(SYSTEM|ASSISTANT|USER)\s*:", re.IGNORECASE)),
    ("null_byte", re.compile(r"\x00")),
    ("repeated_chars", re.compile(r"(.)\1{199,}")),
]

# llm_client.py:69 — duplicate, this one is actually used
def _check_injection(self, user_message: str) -> None:
    _INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("role_prefix", re.compile(r"^\s*(SYSTEM|ASSISTANT|USER)\s*:", re.IGNORECASE)),
        ("null_byte", re.compile(r"\x00")),
        ("repeated_chars", re.compile(r"(.)\1{199,}")),
    ]
    ...
```

**Two issues here:**

1. The orchestrator's copy is dead — delete it.
2. The live copy is in the *adapter*. That means a security-relevant
   input check is tied to the LiteLLM adapter; if you ever swap to a
   different adapter (or the test `FakeLLMPort`), the check disappears
   silently. **Prompt-injection screening is a domain concern, not a
   provider concern.** It should run in the orchestrator before
   `self._llm.complete(...)` — that's the domain saying "I refuse to
   send this to *any* downstream LLM."

The `_INJECTION_PATTERNS` constant probably *was* in the orchestrator
originally, got moved to the adapter, and the original declaration was
never deleted. Fix: keep the patterns and `_check_injection` call in
the orchestrator (run before `self._llm.complete`); delete from
`LiteLLMClient`.

### Duplicated `_check_token_limit`

The same logic appears in `Orchestrator._check_token_limit` (line 697)
and `LiteLLMClient._check_token_limit` (line 87). Same formula
(`len(text) // chars_per_token`), same exception, same numbers. Same
question as injection: which layer owns it?

The token-limit check is "we refuse to send something that won't fit"
— that's a domain policy, not a provider policy, *unless* different
providers have different limits (which is true for LiteLLM, since
different routed models have different context windows). If the limit
is per-route, the adapter should own it; if the limit is a domain-level
"we don't analyze documents bigger than X" guarantee, the orchestrator
should own it. **Pick one.** Right now, the orchestrator-side check
runs first, so the adapter-side one is unreachable for cases the
orchestrator already rejected — making the adapter-side check dead in
practice but live in case the orchestrator path changes.

### Stale import-linter TODO (mentioned in Q2)

The `tenacity` TODO in `pyproject.toml` references code that no longer
exists. Drop the `allow_indirect_imports`-equivalent and tighten the
contract.

### Domain layer is anemic relative to the orchestrator's heft

The 723-line orchestrator contains a lot of logic that arguably belongs
in domain:

- Prompt templates (`_SYSTEM_MESSAGE_TEMPLATE`,
  `_DEFAULT_SUMMARIZATION_PROMPT`, `_DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT`,
  `_JUDGE_PROMPT`) — these are domain *policy* (how the humanitarian
  org talks to its analytical assistant), not service *mechanics*.
- The `_ScoredCode` dataclass with `confidence_aggregate` and
  `explanation` properties — this is a domain value object computing a
  domain invariant.
- `_parse_judge_quality_score` — this enforces a domain rule
  (0.0 ≤ score ≤ 1.0).

Greenfield, I'd push these into `qfa.domain` (e.g., a
`qfa.domain.prompts` module, `qfa.domain.scoring` module). The
orchestrator becomes coordination only. This is a separate improvement
from the per-use-case service split in §6.1, but they reinforce each
other: smaller services + richer domain = each layer doing its real job.

---

## Recommended action list (from cheap to expensive)

1. **[5 min]** Delete dead `_INJECTION_PATTERNS` from
   `orchestrator.py:120-124`.
2. **[5 min]** Drop the stale `tenacity` TODO in `pyproject.toml`; add
   `tenacity` to `forbidden_modules` for `qfa.services`.
3. **[15 min]** Move prompt-injection check out of `LiteLLMClient` into
   the orchestrator. Decide whether token-limit check should live in
   one layer only.
4. **[30 min]** Move `services/call_context.py` into `qfa.domain` (or
   ensure no inverted import). Update one import in
   `adapters/tracking_llm.py`.
5. **[1–2 hours]** Replace `coding_framework: dict[str, Any]` with a
   typed `CodingFramework` Pydantic model. Adjust API schema and
   orchestrator traversal accordingly.
6. **[½–1 day]** Decompose `Orchestrator` into per-use-case services.
   Push prompt templates and scoring helpers into
   `qfa.domain.prompts` / `qfa.domain.scoring`.
7. **[strategic decision, then 1–2 days]** If the team agrees on the
   "Option B" reshape from Q6, redesign the LLM seam as a set of
   domain-shaped ports. Otherwise, leave `LLMPort` as-is and document
   the explicit choice (probably as ADR-012).

Items 1–4 cost less than half a day combined and remove every smell
flagged here that isn't a strategic call. Items 5–6 are the work that
actually matters for "is the implementation too complex?" — they'll
*reduce* the orchestrator's apparent complexity by extracting the
right concepts, not by deleting code.

Item 7 is the only one where a senior architect's judgment beats mine;
it depends on what the team expects to change in the next 12–18 months.

---

## What this review does *not* claim

- I did not run the tests. All conclusions are based on static reading
  of the code in the worktree.
- I read the full domain, services, adapters, and `api.app`/`api.routes`,
  but I read `api.dependencies`, `api.routes_usage`, `api.schemas`,
  `api.schemas_usage`, `cli.migrate`, and the test suites only via
  grep/listing. A deeper API-layer review might surface more.
- I did not look at the `docs/adr` directory, which probably documents
  some of the choices critiqued here. ADR-001/-002/-011 are referenced
  in code comments and clearly exist; if any of them already considered
  the trade-offs above and made an explicit choice the other way, my
  recommendation should be treated as a *reopening* of that decision,
  not a discovery of a missed one.
