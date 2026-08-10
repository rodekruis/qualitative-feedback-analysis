## Spec

**What:** Introduce a separate, independently-configurable LLM connection used exclusively for judge calls, and route all four judge call sites to it. The judge connection **inherits every field from the primary `LLM_*` connection unless explicitly overridden**, so enabling a judge model requires only non-secret variables and **no new Key Vault secret or credential provisioning**. When `JUDGE_LLM_MODEL` is unset, judge calls keep using the primary LLM client, so the default behaviour is unchanged.

**Why:** Judge calls currently share the primary generation client (`Orchestrator._llm`), and `LiteLLMClient` binds a single model string at construction (`src/qfa/adapters/llm_client.py:92`). That couples judging to generation — the self-assessment bias described in the parent epic — and makes it impossible to try a cheaper or independent judge at all.

This ticket delivers **only the mechanism**. Choosing an actual judge model and validating its quality is the sibling ticket (#259) and is explicitly out of scope here — the default configuration after this change must be byte-for-byte the current behaviour.

### Configuration model: per-field inheritance, not a mandatory second credential set

An earlier revision of this spec required a full `JUDGE_LLM_*` credential set (`model` + `api_key` + `api_base` + `api_version`) on the grounds that the judge model was *expected* to sit on a different Azure endpoint (`azure_ai` serverless) than the generation model (`azure` OpenAI). That requirement is dropped, for three reasons:

1. **The premise is unverified, and it is #259's finding, not #258's input.** Whether the judge model lives on a separate endpoint is an outcome of model selection. #258 must not hard-require infrastructure for an endpoint split nobody has confirmed.
2. **The credential — the expensive part — is demonstrably shared; only the route path differs.** This repo has already run the exact candidate judge model as its *primary* model: `LLM_MODEL` was `azure_ai/mistral-medium-2505` until it was swapped to `azure_ai/gpt-5.4` and then `azure/gpt-5.4` (commits `418f5e8`, `0294b90`, 22–23 Jul 2026). The ADR recording that swap (`017-replace-mistral-medium-with-gpt.md`, deleted from the tree by `e5ea6e4` but present in history) states the move stayed on the **same Azure Foundry resource and region (Sweden Central)**, and lists `LLM_API_BASE`, `llm_model` and `AZURE_API_VERSION` as the settings needing an update in every environment — **`LLM_API_KEY` is not in that list**. So the two provider routes on this resource share one API key and differ only in base path: `azure/…` uses `https://<resource>.openai.azure.com/` (`docs/operations/settings-reference.md:13`) while `azure_ai/…` uses the `…services.ai.azure.com/models` inference path. A judge on `azure_ai/mistral-medium-2505` therefore needs a **base-path override but no new credential** — precisely the case per-field inheritance handles well and a mandatory full credential block handles badly.
3. **The deployment cost is real and per-environment.** Following the precedent at `infra/app_service.tf:58-59` and the secret inventory at `infra/key_vault.tf:21-22`, a mandatory credential set means two new Key Vault secrets created and populated by hand in every environment, two more `@Microsoft.KeyVault(...)` `app_settings` entries, and two new terraform variables — before anyone can try a judge model at all.

**The design instead is:** every field of the judge settings block is optional and defaults to `None`, meaning *inherit the corresponding `LLM_*` value*. The overrides still exist, so the separate-endpoint case remains supportable — it is simply no longer mandatory. Note that `LLMSettings.api_key` is a required field (`src/qfa/settings.py:79`); the judge block must **not** inherit that required-ness, which is what forces the optional-field design rather than a plain subclass reusing the parent's field definitions.

The minimum configuration to enable a judge model on the *same* provider route as the primary is one variable:

```
JUDGE_LLM_MODEL=azure/<some-azure-openai-model>
```

For the currently named candidate, which sits on the other provider route of the same Foundry resource, it is two — a model and a base path, both non-secret:

```
JUDGE_LLM_MODEL=azure_ai/mistral-medium-2505
JUDGE_LLM_API_BASE=https://<resource>.services.ai.azure.com/models
```

In terraform both are plain non-secret variables, exactly like the existing `llm_model` (`infra/variables.tf:51`). **Neither is a Key Vault secret**, so no secret has to be created or populated in any environment — which is the cost this design exists to avoid.

### Notes for the implementer

- Judge calls happen at **four** distinct sites, not one:
  - analyze judge — `src/qfa/services/orchestrator.py:449` (structured `AnalyzeJudgeResult`)
  - hierarchical leaf judges — issued through the shared semaphore helper at `src/qfa/services/orchestrator.py:895`, which is *also* used by map calls; only the judge callers may switch clients
  - aggregate-summary judge — `src/qfa/services/orchestrator.py:1189` (`response_model=str`)
  - single-summary judge — `src/qfa/services/orchestrator.py:1269` (`response_model=str`)
- The two summary judges use a free-text contract: a bare float parsed off the first line by `_parse_judge_quality_score` (`src/qfa/services/orchestrator.py:189`), which raises `AnalysisError` on anything malformed. The analyze judge uses a provider-safe structured response format instead. Routing must not change either contract.
- **Cost tracking requires wrapping the judge client too.** The primary client is wrapped in `TrackingLLMAdapter` inside the FastAPI lifespan (`src/qfa/api/app.py:634`), not inside `build_llm_client`. A judge client that is built and injected without the same wrap means usage and cost recording **silently skips every judge call**. Both clients must be wrapped identically.
- Judge field resolution (judge value if set, else primary value) belongs in one place at composition, before the second client is built — `src/qfa/api/composition.py:159` is the current wiring point. Do not scatter `or primary.x` fallbacks across call sites.
- The judge client inherits `timeout_seconds`, `max_total_tokens` and `chars_per_token` from the primary settings; this ticket adds no new knobs for those.
- Wiring path today: `build_llm_client` (`src/qfa/api/app.py:535`) -> `build_orchestrator` (`src/qfa/api/composition.py:164`) -> `Orchestrator(llm=...)`. The judge block hangs off `AppSettings` (`src/qfa/settings.py:325`) alongside `llm`.
- `azure_ai/mistral-medium-2505` pricing is **already registered** in `src/qfa/resources/model_prices.yaml`, so no pricing work is required for that candidate.
- Considered and rejected: adding a per-call `model` override to `LLMPort.complete`. It spreads provider concerns across the port interface and forecloses the separate-endpoint case entirely. Two clients is the right shape.
- **No prior mechanism to reuse.** There has never been a "fast model / smart model" or model-tier mechanism in this repo — no such identifier appears anywhere in git history, and `LLMSettings` has exactly one consumer (`build_llm_client`). Every past multi-model episode was a *sequential swap of the single model string*, not two concurrent models. #258 is the first concurrent-two-model mechanism, so it is building the mechanism, not hooking into one.
- **This follows ADR-004, it does not contradict it.** ADR-004 ("Single LLM Client for All Providers", Accepted, `docs/adr/004-single-llm-client.md`) decides that one client *class* serves all providers and that **provider/model selection happens at startup in the composition root, not inside the client**. Two `LiteLLMClient` instances differing by model and base is exactly that pattern; no new ADR is needed. Note ADR-004's prose is stale in its specifics — it predates LiteLLM and still describes injecting `AsyncOpenAI`/`AsyncAzureOpenAI` into a `services/llm_client.py`; the principle is what applies, not the described wiring.
- **`.env.example:7` is stale and should be corrected while in here:** it still carries the pre-swap `LLM_API_BASE=https://....services.ai.azure.com/models` (the `azure_ai` route) alongside `LLM_MODEL=azure/gpt-5.4` (the `azure` route), contradicting `docs/operations/settings-reference.md:13`. Left as-is it will mislead anyone configuring the judge base path by copying the primary's.

## Acceptance criteria

- [ ] A judge LLM settings block with env prefix `JUDGE_LLM_` exists alongside `LLMSettings` (`src/qfa/settings.py:68`), exposing at least `model`, `api_key`, `api_base`, `api_version`, **with every field optional and defaulting to `None`** — no field of the judge block is independently required.
- [ ] **Enabling a judge model requires no new secret and no Key Vault change:** with only `JUDGE_LLM_MODEL` set, the judge client resolves `api_key`, `api_base` and `api_version` from the primary `LLM_*` settings — covered by a test at the settings-resolution level. (A judge on a different provider route additionally sets the non-secret `JUDGE_LLM_API_BASE`; `JUDGE_LLM_API_KEY` stays inherited.)
- [ ] Each `JUDGE_LLM_*` field, when explicitly set, overrides the inherited primary value for that field only, leaving the others inherited — covered by a per-field test.
- [ ] With `JUDGE_LLM_MODEL` unset, every judge call uses the existing primary client and the same model as today — covered by a test asserting no change in the model used for judge calls.
- [ ] With `JUDGE_LLM_MODEL` set, a second `LLMPort` is constructed from the resolved judge settings and injected into `Orchestrator` as a distinct judge client, without mutating or replacing the primary client.
- [ ] All four judge call sites listed above issue their call on the judge client.
- [ ] Non-judge calls (analysis, hierarchical map, reduce, coding classification, summary generation) still use the primary client — covered by a test asserting that setting a judge model does not change the model serving generation calls.
- [ ] The hierarchical semaphore helper (`orchestrator.py:895`) routes judge callers to the judge client while map callers stay on the primary client; the single shared semaphore, timeout, and deadline behaviour are unchanged.
- [ ] Cost and token accounting remain correct with two models in play: **the judge client is wrapped in `TrackingLLMAdapter` on the same composition path as the primary** (`src/qfa/api/app.py:634`), `LLMResponse.model` reflects the model that actually served each call, and totals sum across both clients — covered by a test asserting judge-call usage is recorded.
- [ ] No new startup failure mode is introduced. Because unset judge fields inherit from the primary, a `JUDGE_LLM_MODEL` set on its own is a valid, complete configuration and must start successfully.
- [ ] `JUDGE_LLM_*` variables are documented in `docs/operations/settings-reference.md` and added to `.env.example`, **documenting the inheritance rule explicitly** (unset field = inherit from `LLM_*`) and showing the one-variable minimum configuration.
- [ ] Architecture documentation shows the judge client as a separate outbound LLM dependency (`docs/architecture/03-components.md` and any diagram that renders the single LLM edge).
- [ ] Existing test suites pass unchanged, and new tests cover the unset-fallback, per-field inheritance, and per-call-site routing.

### Out of scope

- Adding a `judge-llm-api-key` Key Vault secret. The judge inherits the primary key; a separate judge *credential* is only needed if #259 selects a model on a different Azure resource or a different provider entirely, which is not the current candidate.
- Adding terraform variables / `app_settings` entries for the judge. #258 delivers the mechanism and its env-var contract; wiring the chosen values into `infra/` belongs with #259, which decides what those values are. When it happens, `judge_llm_model` and `judge_llm_api_base` are plain non-secret variables like `llm_model`.
- Restoring the deleted ADR-017 (see below) — worth doing, but not this ticket's job.
- Selecting or evaluating the judge model itself (#259).
