BLOCKED-check passed — every file, module and API the spec names exists in this tree. Plan follows.

---

# Implementation plan — #223: App Insights `App*` tables flatline at zero

## 0. Root cause (found in-tree; not a hypothesis)

The spec lists four hypotheses. **All four are wrong**, and the two that were checkable from the paper trail are eliminated below. The actual cause is three independent code-level defects, which together explain why *all four* `App*` tables are empty simultaneously — something none of the spec's hypotheses predicts.

**Hypotheses eliminated without Azure access:**

- **H1 (deployed image predates the OTel wiring) — dead.** `configure_azure_monitor()` landed in `f3d80bf` (2026-07-01). `scripts/show_deployed_versions.sh` reports dev running `ce78c1c` (2026-09-08, "Build from commit"), a descendant. `git merge-base --is-ancestor f3d80bf v2.4.0` also passes, so staging/prd on `v2.6.0` contain it too.
- **H3 (connection string not resolving) — near-dead.** `APPLICATIONINSIGHTS_CONNECTION_STRING` was added to `infra/app_service.tf` in `572973b` (2026-06-23) and is present in `f28e0752`, the commit of the last recorded Terraform apply (2026-07-29). Terraform therefore sets it. Step 8 still confirms it at runtime, cheaply.
- **H2 (crash-loop loses batches) / H4 (egress blocked)** cannot produce a *sustained* flatline while requests succeed, and both would leave exporter errors in `AppServiceConsoleLogs` and kill Live Metrics.

**Defect 1 — no `AppRequests`/`AppExceptions`: the FastAPI instrumentation never attaches.**

`FastAPIInstrumentor._instrument()` (opentelemetry-instrumentation-fastapi 0.61b0, the version azure-monitor-opentelemetry 1.8.8 pins) works by rebinding a module attribute:

```python
def _instrument(self, **kwargs):
    self._original_fastapi = fastapi.FastAPI
    fastapi.FastAPI = _InstrumentedFastAPI      # ← module-attribute swap
```

`src/qfa/main.py:8` does `from qfa.api.app import create_app` **before** `configure_azure_monitor()` at `src/qfa/main.py:25-31`. That import runs `src/qfa/api/app.py:10` — `from fastapi import FastAPI` — which binds `qfa.api.app`'s module global to the *original* class. `create_app()` instantiates that stale binding at `src/qfa/api/app.py:996`, so the OTel ASGI middleware is never installed and no server spans exist. The comment at `main.py:20-22` shows the author knew the *call* had to be ordered; the from-import binding defeats it.

**Defect 2 — no `AppTraces`: the OTel log handler is torn off the root logger.**

`azure/monitor/opentelemetry/_configure.py::_setup_logging` attaches a `LoggingHandler` (a bare `logging.Handler` subclass) to the **root** logger (default `logger_name=""`). `qfa.utils.setup_logging` (`src/qfa/utils.py:65-67`) then calls `logging.basicConfig(..., force=True)`, which *removes and closes* every existing root handler. It is called from the lifespan at `src/qfa/api/app.py:838` — i.e. after `configure_azure_monitor()`. The comment at `utils.py:59-64` documents the interaction in one direction only; the OTel handler is collateral.

**Defect 3 — no `AppDependencies`, so the Application Map cannot render.**

`_ALL_SUPPORTED_INSTRUMENTED_LIBRARIES` in the distro is exactly `(azure_sdk, django, fastapi, flask, psycopg2, requests, urllib, urllib3)`, and `_setup_instrumentations` `continue`s on anything else — even if its entry point is installed. This app talks to Postgres via **asyncpg** and to the LLM via **httpx** (through litellm). Neither is instrumented. AC #3 cannot pass without adding instrumentation explicitly.

---

## 1. Decisions the spec left open

| Decision | Choice | Why |
|---|---|---|
| Fix Defect 1 by reordering `main.py`'s imports, or by explicit instrumentation? | **Explicit**: `FastAPIInstrumentor.instrument_app(app)` after `create_app()`. | Reordering works but is invisible-fragile — ruff's `E` ruleset flags `E402`, so it needs a `noqa`, and any future import tidy silently re-breaks telemetry with no test able to catch it. `instrument_app` is the documented manual path, is idempotent (guards on `_is_instrumented_by_opentelemetry`), and is directly assertable in a unit test. |
| Postgres spans via `opentelemetry-instrumentation-asyncpg` or `-sqlalchemy`? | **`-sqlalchemy`, with `instrument(engine=async_engine.sync_engine)`.** | **asyncpg instrumentation is a trap here.** It wraps only `Connection.execute/executemany/fetch/fetchval/fetchrow`. SQLAlchemy's asyncpg dialect issues statements via `Connection.prepare()` + `PreparedStatement.fetch()` (`sqlalchemy/dialects/postgresql/asyncpg.py:755,773`), which are *not* wrapped — it would ship and produce almost no spans. Do not substitute it. |
| Global `SQLAlchemyInstrumentor().instrument()` or per-engine? | **Per-engine.** | The global form patches `sqlalchemy.ext.asyncio.create_async_engine`, and `src/qfa/adapters/db.py` from-imports that symbol — the identical rebind trap as Defect 1. |
| Where does the bootstrap live? | New module **`src/qfa/telemetry.py`**, three functions. `qfa.main` keeps only the import-time *call* (the only hook gunicorn gives us). | Today the logic is import-time side-effect code and untestable. `qfa.telemetry` is a top-level sibling of `qfa.settings`/`qfa.utils`, so it sits outside the `layers` contract (which is not `exhaustive`) — no contract edits needed. |
| Fix Defect 2 inside `qfa.utils` with an `opentelemetry` import? | **No** — preserve handlers structurally, by type. | `qfa.services.analyze:62` imports `qfa.utils`; adding an SDK import there drags infrastructure into the services import graph, against the spirit of the "services depend only on ports" contract. Preserving "every root handler that is not a `StreamHandler`" achieves the same thing with zero new dependencies and is testable with a plain sentinel handler. |
| Do we remove the "Verification pending" note (AC #4) in this PR? | **Conditionally**, see Step 9. The build agent has no `az` CLI and no Azure credentials in this environment. | Removing it on the strength of local tests alone would report unverified work as verified. Step 9 spells out the exact gate. |
| Exclude `/v1/health` from tracing? | **No.** | `always_on` + `health_check_path` means the platform probe hits it roughly every minute, which is exactly the free, continuous signal AC #2 needs. Documented in Step 7 along with `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` as the lever if the volume becomes a cost problem. |

**Discriminating prediction, stated up front so Step 8 is falsifiable:** `enable_live_metrics` defaults to `True` (`azure/monitor/opentelemetry/_utils/configurations.py:334`). If this diagnosis is right, dev **today** shows Live Metrics *connected* with performance counters ticking but a flat 0 request rate. If it shows "not connected", the diagnosis is wrong and H2/H3/H4 are back in play — stop and re-diagnose rather than proceeding.

---

## 2. Ordered steps

Steps 1–7 are the PR. Step 8 is a pre-flight observation that can be done any time. Step 9 is post-merge and needs Azure access.

### Step 1 — Dependencies (`pyproject.toml`, `uv.lock`)

Add to `[project] dependencies`:

```
"opentelemetry-instrumentation-httpx==0.61b0",
"opentelemetry-instrumentation-sqlalchemy==0.61b0",
```

and tighten `"azure-monitor-opentelemetry>=1.8.8"` → `"azure-monitor-opentelemetry~=1.8.8"`.

**Exact pins are mandatory, not stylistic.** azure-monitor-opentelemetry 1.8.8's metadata pins every instrumentation it owns at `==0.61b0`, and all of them depend on `opentelemetry-instrumentation==0.61b0`. An unpinned add resolves to 0.65b0 and fails to co-resolve. Add a one-line comment next to the pins recording that they must move in lockstep with the distro, and that this is why the distro is now `~=`.

Run `uv lock` (not `--upgrade`). Verify: `uv sync --all-extras` succeeds, and `make test` still passes unchanged before touching any source.

### Step 2 — `src/qfa/telemetry.py` (new)

Three functions, no import-time side effects (`make test-doctest` runs `pytest --doctest-modules src`, and `make docs` autosummarises `qfa` recursively — both import this module bare):

- `configure_telemetry(settings: TelemetrySettings | None = None) -> bool` — constructs `TelemetrySettings()` when not given; returns `False` and does nothing when `applicationinsights_connection_string` is unset. Otherwise imports `configure_azure_monitor` locally (keep the existing lazy-import style), calls it with `connection_string=...get_secret_value()`, then calls `HTTPXClientInstrumentor().instrument()`, and returns `True`. httpx belongs here because it is process-global, needs no instance, and is order-safe: it wraps `httpx.HTTPTransport.handle_request` / `AsyncHTTPTransport.handle_async_request` as class attributes, so clients litellm created at import time are still traced.
- `instrument_app(app, *, tracer_provider=None) -> None` — `FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)`. The `tracer_provider` keyword exists for the test in Step 5; production passes nothing and takes the global.
- `instrument_db_engine(engine: AsyncEngine, *, tracer_provider=None) -> None` — `SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=tracer_provider)`. `engine.sync_engine` is the form the instrumentation's own module docstring prescribes for async engines.

Docstrings must earn their keep per AGENTS.md. The one added fact worth stating, on `instrument_app`: it overrides `build_middleware_stack` on the instance, so it must be called before the app serves its first request, and it must not be relied on to happen implicitly via the class patch — link #223.

Type-hint `FastAPI`/`AsyncEngine` under `TYPE_CHECKING` so this module stays import-light and cannot itself become part of an ordering trap.

### Step 3 — `src/qfa/main.py`

Replace the import-time block at lines 15–31 with:

```python
_telemetry_enabled = configure_telemetry()
app = create_app()
if _telemetry_enabled:
    instrument_app(app)
```

Keep `configure_telemetry()` at module scope: under `entrypoint.sh` (`gunicorn qfa.main:app`) the module import is the only startup hook. The gating means local dev is untouched. Trim the 12-line comment down to the facts that survive the change (why it is at module scope; why `TelemetrySettings` is safe to build standalone) and drop the now-wrong claim that ordering `create_app()` after the call is sufficient.

### Step 4 — `src/qfa/utils.py::setup_logging`

Around `logging.basicConfig(..., force=True)` (lines 65–67):

```python
root = logging.getLogger()
preserved = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]
for handler in preserved:
    root.removeHandler(handler)
logging.basicConfig(level=..., force=True, **log_config.basicConfig)
for handler in preserved:
    root.addHandler(handler)
```

Detaching *before* `basicConfig` is load-bearing: `force=True` closes what it removes, and a closed exporter handler is unrecoverable. `logging.FileHandler` subclasses `StreamHandler`, so basicConfig-style output handlers are still replaced — behaviour preserved. Rewrite the existing comment to say what it now does (replace our own stdout handler; leave third-party sinks alone) instead of what the old code did.

### Step 5 — `src/qfa/api/app.py` lifespan

At line 851, immediately after `engine = create_async_engine_from_settings(settings.db)` and before `create_session_factory(engine)`:

```python
if settings.telemetry.applicationinsights_connection_string:
    instrument_db_engine(engine)
```

Gate on the settings value rather than module state — the lifespan already holds `settings`, and it keeps the function stateless and directly testable. `qfa.api.app -> qfa.telemetry` needs no `ignore_imports` entry (verify with `make lint_imports`; if a contract does object, add `qfa.telemetry` as a top-level sibling, never into the layer stack).

### Step 6 — Tests

**Ordering constraint:** `HTTPXClientInstrumentor` and `SQLAlchemyInstrumentor` mutate process-global state. Every test that instruments must uninstrument in fixture teardown, or it leaks into the rest of the suite and produces order-dependent failures. Likewise snapshot and restore `logging.getLogger().handlers` in the logging tests.

**Refactor first:** move the `app_env` fixture from `tests/api/test_lifespan.py:56` into `tests/api/conftest.py` so both `test_lifespan.py` and the new module can use it. No behaviour change.

`tests/test_telemetry.py` (new):

1. `test_configure_telemetry_is_a_noop_without_a_connection_string` — `delenv`; patch the SDK entry point; assert not called and returns `False`.
2. `test_configure_telemetry_passes_the_connection_string_to_the_sdk` — `setenv`; assert called once with `connection_string=<value>` and returns `True`.
3. `test_configure_telemetry_instruments_httpx` — assert `HTTPXClientInstrumentor().is_instrumented_by_opentelemetry` after the call; uninstrument in teardown.
4. **`test_instrument_app_does_not_depend_on_the_fastapi_class_patch`** — the regression test for Defect 1. Assert `type(app) is fastapi.FastAPI` (i.e. the class was never swapped) *and* `app._is_instrumented_by_opentelemetry is True` after `instrument_app(app)`. Docstring cites #223: the previous code produced zero `AppRequests` precisely because it relied on the swap.
5. **`test_instrumented_app_records_a_server_span_per_request`** — the end-to-end proof. `TracerProvider` + `SimpleSpanProcessor` + `InMemorySpanExporter`; `app = create_app(llm_factory=_RecordingFakeLLM)`; `instrument_app(app, tracer_provider=provider)`; then inside `app.router.lifespan_context(app)` drive `GET /v1/health` over `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`. Assert one span with `kind is SpanKind.SERVER` and `attributes["http.route"] == "/v1/health"`. `instrument_app` must run before the first request — Starlette builds the middleware stack lazily on first call.
6. `test_instrument_app_is_idempotent` — call twice; assert no error and exactly one OTel middleware layer in `app.user_middleware`.
7. **`test_sqlalchemy_spans_do_not_record_bound_parameters`** — data-classification guard, and it needs no Postgres: `create_engine("sqlite://")` (stdlib `sqlite3`), `instrument_db_engine`-equivalent on the sync engine, execute a parameterised statement with a sentinel value, and assert the sentinel appears in **no** span attribute while `db.statement` carries the placeholder form.

`tests/test_utils.py` (extend):

8. `test_setup_logging_preserves_non_stream_root_handlers` — attach a bare `logging.Handler` subclass that records `close()`; call `setup_logging()`; assert it is still attached and was never closed.
9. `test_setup_logging_still_replaces_the_stream_handler` — two consecutive calls leave exactly one `StreamHandler` on root.

`tests/api/test_lifespan.py` (extend, reusing the moved `app_env`):

10. `test_db_engine_is_instrumented_when_telemetry_is_configured` — set `APPLICATIONINSIGHTS_CONNECTION_STRING`; monkeypatch `qfa.api.app.instrument_db_engine` with a recorder; run the lifespan; assert called once and that the argument's `.sync_engine` is `app.state`'s engine's `sync_engine`.
11. `test_db_engine_is_not_instrumented_without_telemetry` — `delenv`; assert not called.

Gate: `make test` and `make lint` (ruff + ty + import-linter) green, and `make docs` green under `-W` with the new module autosummarised.

### Step 7 — Documentation

`docs/operations/observability.md` — this is a behaviour addition, so growth is allowed, but keep it terse and delete the prose it replaces:

- Remove the two inline "delivery pending verification" parentheticals (lines 229, 247). Handle the blockquote at line 355 per Step 9.
- Add a four-row table under **Application Insights (application telemetry)** mapping each `App*` table to what populates it: `AppRequests`/`AppExceptions` ← FastAPI ASGI instrumentation on the app instance; `AppDependencies` ← SQLAlchemy (Postgres) + httpx (LLM); `AppTraces` ← the OTel root-logger handler. This is the troubleshooting checklist the removed note should have been.
- Add: `AppRequests` is **rate-limit sampled at 5 spans/sec** by default (`azure-monitor-opentelemetry`), so it is *not* a complete request log — `AppServiceHTTPLogs` remains the authoritative one. This matters and is easy to misread as data loss.
- Add: the platform health probe traces `/v1/health` roughly every minute, which is why an idle dev environment still shows a request rate; `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` is the lever if the volume becomes a cost concern.
- Add one line to the **Hard prohibitions** area: dependency spans record parameterised SQL (`db.statement`) and outbound endpoint URLs — never bound parameters, feedback text, prompts, or model output — pointing at test 7 as the guard.

`docs/security-brief.html` — required by AGENTS.md, and this genuinely changes the data-flow picture: application log lines now reach a second sink. Add one bullet to **Audit & Monitoring** (line 299): application telemetry (requests, dependencies, exceptions, log lines) is exported to Application Insights within the same Azure subscription — not a third-party log analytics service — and dependency records carry parameterised SQL and endpoint URLs only.

`infra/app_service.tf` — **optional, flagged for reviewer judgement.** Add `ApplicationInsightsAgent_EXTENSION_VERSION = "disabled"` to `app_settings`. The distro emits an explicit attach warning when codeless auto-instrumentation is on, and the docs already carry a prose "Don't click Enable there" warning; this pins that state in code instead. It is a no-op if codeless was never enabled, but it does require a Terraform apply — drop it if you would rather not couple this PR to an infra apply.

### Step 8 — Pre-flight observation (no Azure write access needed; portal read only)

Do this before or alongside the PR, and record the answer in the PR body. Open `qfa-dev-appinsights` → **Live Metrics** while curling `/v1/health`.

- **Connected, performance counters ticking, request rate flat 0** → confirms the diagnosis above. Proceed.
- **"Not connected"** → the diagnosis is wrong. Do not ship blind; return to the spec's diagnostic order steps 3–4 (`AppServicePlatformLogs` for `ContainerTimeout`/OOM, `AppServiceConsoleLogs` for exporter errors, and `az webapp config appsettings list` to confirm the connection string is non-empty at runtime) and re-plan.

### Step 9 — Post-merge live verification (requires Azure access — the build agent in this environment has neither `az` nor credentials)

After the fix deploys to dev, fire a handful of requests including one that hits Postgres and one that calls the LLM, wait out the 2–5 minute ingestion lag, then:

```kql
AppRequests     | where TimeGenerated > ago(15m) | project TimeGenerated, Name, ResultCode, DurationMs
AppDependencies | where TimeGenerated > ago(15m) | project TimeGenerated, Type, Target, Name, Success
AppTraces       | where TimeGenerated > ago(15m) | project TimeGenerated, Message
```

Expect `AppRequests` rows for `/v1/health`, `AppDependencies` rows with `Type` `postgresql` **and** `HTTP` (the LLM endpoint), `AppTraces` carrying the app's own log lines, and the Application Map drawing app → Postgres → LLM.

**Residual uncertainty to check here, not before:** httpx instrumentation wraps the standard `AsyncHTTPTransport`. If litellm's Azure path substitutes a transport subclass that overrides `handle_async_request`, LLM calls will be missing from `AppDependencies` while Postgres calls are present. That specific split symptom is the signal to add a litellm/OpenAI-SDK-level instrumentation instead — it is not a reason to redo Steps 1–7.

**AC #4 gate.** Remove the "Verification pending" blockquote (`docs/operations/observability.md:355`) *only* once this step passes, replacing it with a one-line "Verified against dev on `<date>` — see #223." If the person running the build cannot reach Azure, leave the blockquote in place, rewritten to name the three fixed defects and point at the KQL above, and say plainly in the PR body that live end-to-end delivery is unverified. Do not delete the note on the strength of the local tests alone.

---

## 3. Ordering constraints

1. Step 1 before Steps 2–5 — the new imports do not resolve otherwise.
2. Within `qfa.main`: `configure_telemetry()` → `create_app()` → `instrument_app(app)`. The provider must be global before `instrument_app` resolves a tracer, and `instrument_app` must precede the first request.
3. Step 5 must land with Step 2 — `instrument_db_engine` has no other caller.
4. Step 4 is independent of Steps 2/3/5 and could ship alone; keep it in the same PR since it is one of the three causes of the single reported symptom.
5. Step 8 before merge (it is the falsification check). Step 9 strictly after deploy.

## 4. Explicitly out of scope

- A `server_request_hook` stamping `X-Request-ID` onto the server span so App Insights Transaction Search joins to the existing log/`llm_calls` correlation. Genuinely valuable, not required by any AC — open a follow-up.
- LLM-semantic-convention (`gen_ai.*`) span attributes for token/cost telemetry. `GET /v1/usage` already covers this from Postgres.
- Any change to the alert rules or the diagnostic setting in `infra/observability.tf` — validated in #207 and unaffected.
