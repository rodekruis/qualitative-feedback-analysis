# Implementation plan — #309 (analyze-bulk judge 400 on `azure_ai/mistral-medium-3-5`)

## What I found before planning, and what it changes

I read the real tree and the vendored `litellm` 1.84.0 source (the version pinned in `uv.lock`). Two findings change the shape of the work, so state them up front:

1. **The spec's suspected cause is unlikely to be the whole story.** `litellm.utils.type_to_response_format_param` delegates to openai's `to_strict_json_schema`, which already emits a textbook-strict payload. For `AnalyzeJudgeResult` the wire schema after `_provider_safe_response_format` is: root `type: object`, `additionalProperties: false`, `required` listing both properties, each property carrying only `title` + `type`, wrapped as `{"type":"json_schema","json_schema":{"schema":…,"name":"AnalyzeJudgeResult","strict":true}}`. Nothing in `$defs`, no `$ref`, no constraint keywords (they are stripped). There is no obviously-rejectable keyword left to strip, so "add another entry to `_UNSUPPORTED_SCHEMA_KEYWORDS`" cannot be planned as *the* fix — we do not know which entry.
2. **There is a second, better-supported cause family, and litellm ships the mitigation.** `azure_ai/*` (non-Claude, non-agents) routes through `base_llm_http_handler`, whose 400 path consults `AzureAIStudioConfig.should_retry_llm_api_inside_llm_translation_on_http_error` (`litellm/llms/azure_ai/chat/transformation.py:272`). That hook retries the request with the offending fields removed when the provider body says `"Extra inputs are not permitted"` — **but only when `drop_params` is truthy**, which it is not for us (we never set it, and `litellm.drop_params` defaults to `False`). Azure AI Foundry's `/models/chat/completions` route is exactly the endpoint known to reject extra top-level fields; we send `user=tenant_id` on every call, and it is the judge connection's first exposure to that route. Two of the three markers that hook recognises (`Set extra-parameters to 'pass-through'`, `unknown field: parameter index`) retry unconditionally; the `Extra inputs are not permitted` one needs the flag.

Neither cause can be confirmed in this repo: there are no Azure credentials and no network path to the provider from CI or from a build agent. **So this PR is diagnose-and-mitigate, not confirm-and-fix**, and it must say so in its own text. The steps below are ordered so that the diagnostic ships regardless of whether the mitigation happens to be the cure.

## Decisions I made that the spec left open

- **D1 — The "capture the 400 body" step becomes a product change, not a manual one-off.** The spec's step 1 assumes a human at a terminal with `litellm._turn_on_debug()`. A build agent cannot do that, and a one-off capture leaves the next incident just as blind. Instead: ship a permanent, content-free diagnostic log line on the bad-request path, plus a documented runbook for the raw-body capture a human *can* run in dev-test.
- **D2 — The diagnostic logs a closed-vocabulary classification, never provider text.** ADR-018 and `docs/operations/observability.md` forbid provider-derived strings in logs; `tests/adapters/test_llm_client.py::TestProviderTextContainment` enforces it with a canary. The precedent for reading a provider string without propagating it is already in this file (`_to_domain_error`'s content-filter sniff, and `_content_filter_signal`). Follow it: match the message against a fixed vocabulary of JSON-Schema keyword names and log the matched token or `unknown` — never the message.
- **D3 — Pass `drop_params=True` per call, not `litellm.drop_params = True` globally.** Per-call keeps the setting visible at the one call site that makes it, keeps it out of process-global state shared with anything else importing litellm, and is directly assertable in a unit test. It is a supported `acompletion` kwarg and lands in `litellm_params` (`litellm/litellm_core_utils/get_litellm_params.py:146`), which is where the retry hook reads it.
- **D4 — Do not add speculative entries to `_UNSUPPORTED_SCHEMA_KEYWORDS`, and do not inline `$defs`/`$ref`.** Both were tempting. Neither is justified by evidence in the tree: the judge model's schema has no `$defs`, so ref-inlining fixes nothing observable here and would be work done on behalf of #299/#310, which are explicitly out of scope. Adding keywords by guesswork degrades the schema the model sees for no known gain. The conformance test in step 1 is where a future confirmed keyword gets pinned.
- **D5 — No free-text/unstructured fallback for the judge call.** A "retry the judge without `response_format` and parse a bare float" path would guarantee a score, but it duplicates the exact fragile parsing that #299 exists to remove, and #299 is out of scope for this issue. Not built.
- **D6 — The immediate P1 mitigation is operational and is not this PR.** ADR-020's own rollback condition 3 ("a sustained rise in `Analyse judge call failed` warnings") has fired. Setting `AZ_JUDGE_LLM_MODEL=""` for the affected environment and re-applying restores confidence scores today with no code change or image redeploy. Surface this in the PR body as a recommendation for a human to decide; **do not touch infra or apply anything yourself.**
- **D7 — `AnalyzeJudgeResult`'s `Field` constraints stay exactly as they are.** They are stripped at the boundary and are the authoritative check on parse. Do not "fix" the model by removing `ge`/`le`/`min_length`.

## Steps

### 1. Pin the judge call's wire schema against the Azure-AI-Mistral strict contract *(test-only, do first)*

File: `tests/adapters/test_llm_client.py`, extending the existing `TestProviderSafeResponseFormat` class.

Add a module-level helper `_assert_mistral_strict_schema_contract(response_format: dict) -> None` asserting, recursively where noted:

- `response_format["type"] == "json_schema"`; `json_schema["strict"] is True`; `json_schema["name"]` matches `^[A-Za-z0-9_-]{1,64}$`
- schema root is `type: "object"`
- every object node has `additionalProperties is False` and a `required` list equal to its `properties` keys
- no key from `_UNSUPPORTED_SCHEMA_KEYWORDS` appears anywhere (json-dump membership check, as the existing tests do)
- no `$ref` / `$defs` anywhere

Then add `test_judge_response_format_meets_azure_ai_mistral_contract`, which imports `AnalyzeJudgeResult` from `qfa.services.analyze`, runs it through `_provider_safe_response_format`, and applies the helper. Importing a service model into an adapter test is fine: `import-linter` has `root_packages = ["qfa"]` and does not analyse `tests/`. Keeping the checker in one place next to the existing sanitiser tests is worth more than test-layer purity.

This is a characterization test — it passes on today's code. That is the point: it is the baseline that steps 2–3 must not disturb, and it is where the *next* confirmed Mistral constraint gets encoded. Note in its docstring that `AnalyzeJudgeResult` is the response model for **both** structured judge sites (`analyze.py:266` and the hierarchical leaf judge at `analyze.py:719`), so this one assertion covers both.

**Ordering: must land before steps 2 and 3.**

### 2. Content-free bad-request diagnostics in the LiteLLM adapter

File: `src/qfa/adapters/llm_client.py`.

- Add a module-level `_SCHEMA_KEYWORD_VOCABULARY: frozenset[str]` next to `_UNSUPPORTED_SCHEMA_KEYWORDS` (~line 36–61): a closed set of JSON-Schema/structured-output token names that could plausibly be named in a rejection — the existing unsupported set plus `type`, `title`, `description`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, `format`, `default`, `anyOf`, `oneOf`, `allOf`, `$ref`, `$defs`, `strict`, `name`, `schema`, `response_format`, `user`. It is a vocabulary for *recognising*, not for stripping — keep the two sets clearly separate in the comment so nobody later wires this one into `_strip_unsupported_schema_keywords`.
- Add `_rejected_schema_keyword(message: str) -> str | None`, placed next to `_content_filter_signal` (~line 113). Match Mistral's observed phrasing — `Received unsupported keyword \`minimum\` in schema`, recorded verbatim in commit `24165c8` — with a case-insensitive regex capturing the backticked or quoted token, and **return it only if it is in `_SCHEMA_KEYWORD_VOCABULARY`**; otherwise `None`. Docstring must state the ADR-018 reasoning in one line: reads the provider string to classify, never propagates it.
- In `_complete_once`'s `except` block, keep the existing `logger.error("LLM provider error: …")` line untouched (tests assert it) and add, **only when `isinstance(exc, BadRequestError)`**, a second content-free line:

  ```
  logger.error(
      "LLM provider rejected request: model=%s response_format=%s schema_name=%s "
      "schema_keys=%s rejected_keyword=%s", …
  )
  ```

  where `response_format` is `"json_schema"` or `"none"`, `schema_name` is `response_format["json_schema"]["name"]` (our own model class name — repo-authored), `schema_keys` is the sorted set of dict keys occurring anywhere in the schema we sent (also repo-authored), and `rejected_keyword` is `_rejected_schema_keyword(str(exc)) or "unknown"`. Every field is either authored here or drawn from a closed set.
- Do **not** add the keyword to `LLMBadRequestError`. The error envelope stays as it is; this is a log-only signal.

Tests (same file, new class `TestBadRequestDiagnostics`, plus one addition to `TestProviderTextContainment`):

- `test_logs_rejected_schema_keyword_from_closed_vocabulary` — `BadRequestError(message="Received unsupported keyword \`title\` in schema", …)` → a record contains `rejected_keyword=title`.
- `test_unrecognised_rejection_reason_logs_unknown` — a message with a backticked token outside the vocabulary → `rejected_keyword=unknown`, and that token is absent from every record.
- `test_logs_schema_name_and_keys_for_structured_call` — drive `complete(..., _StructuredResponse)` against a `BadRequestError` side effect; assert `schema_name=_StructuredResponse` and that `schema_keys` includes `properties`/`required`.
- `test_bad_request_diagnostic_logs_no_sentinel` in `TestProviderTextContainment` — mirror of the existing `test_no_log_record_contains_sentinel`, but with `BadRequestError(message=f"Received unsupported keyword \`{SENTINEL}\` in schema")` at `caplog.at_level(logging.DEBUG)`. This is the regression guard on D2; it must fail if anyone later logs the raw body.

**Ordering: after step 1, before step 3** — the diagnostic is what tells the operator whether step 3 worked, so it must not depend on step 3 being right.

### 3. Enable litellm's Azure-AI parameter self-healing on the completion call

File: `src/qfa/adapters/llm_client.py`, in `_complete_once`'s `acompletion(...)` call.

Add `drop_params=True` with a comment stating the added fact: on the `azure_ai/` route a 400 whose body names a rejected top-level field is retried by litellm with that field removed, and that retry is gated on this flag; without it a single rejected field (we send `user`) fails the whole call. Add one line to `complete`'s docstring under the existing retry paragraph — one sentence, no more.

Blast radius, to state in the comment and the PR body: this also applies to primary `azure/` calls. We send only `model`, `messages`, `api_key`, `api_base`, `api_version`, `user`, `timeout`, `response_format` — all in `OpenAIConfig.get_supported_openai_params`, so nothing is dropped pre-flight; the flag only unlocks the reactive path. The worst case is that the provider names `response_format` as the rejected field: the judge then returns prose, `model_validate_json` fails, `LLMResponseParseError` is raised — and it subclasses `LLMError`, which `analyze_bulk`'s `except` tuple already catches (`analyze.py:271-283`), so the degrade-to-`null` behaviour is unchanged. No new failure mode.

Tests:

- `TestLiteLLMClientCallParameters::test_passes_drop_params_so_provider_can_self_heal` — assert `call_kwargs["drop_params"] is True`.
- Re-run the existing `test_passes_correct_params`: it asserts individual keys, not an exact kwarg set, so it should still pass. If it turns out to compare the whole kwargs dict, extend rather than replace it.

### 4. Pin the service-level degradation for this exact error

File: `tests/services/test_analyze.py`, class `TestAnalyzeJudgeFailure`.

Add `test_judge_bad_request_degrades_to_none_score`, identical in shape to the existing `test_judge_failure_returns_none_score_and_unavailable_text` but with `errors=[None, LLMBadRequestError("judge rejected", provider_status=400)]`. It asserts `quality_score is None`, `uncertainty_explanation == JUDGE_UNAVAILABLE_EXPLANATION`, and that the analysis text still comes back — i.e. that the reported 200-with-null-score behaviour is deliberate and stays that way.

The hierarchical leaf-judge site is already covered by `tests/services/test_analyze_hierarchical.py::test_all_judges_failing_yields_none_confidence_with_synthesis`; no change needed there.

### 5. Documentation (same PR — required by AGENTS.md)

- **`docs/operations/observability.md`**
  - Add one bullet to "Safe to log": the bad-request diagnostic's fields (`model`, `response_format`, `schema_name`, `schema_keys`, `rejected_keyword` — a closed vocabulary, like the content-filter `category`/`severity` bullet directly above it).
  - Add a short subsection, "Diagnosing a provider 400" — a numbered runbook, no prose padding: (1) read the `LLM provider rejected request:` line; (2) if `rejected_keyword` names a keyword, add it to `_UNSUPPORTED_SCHEMA_KEYWORDS` and to the step-1 contract test; (3) if it is `unknown`, capture the raw body **in dev-test only, with synthetic feedback only**, by raising `LOG_LOGLEVEL_3RDPARTY` to `debug` (or `litellm._turn_on_debug()`), reproducing once, and reverting immediately — with an explicit warning that litellm's own debug stream prints the assembled messages, so the "Hard prohibitions" list applies and this must never be run against real feedback or a production tenant.
- **`docs/adr/020-mistral-medium-as-judge-model.md`** — two surgical edits, no rewrite of the decision: correct the Follow-up section's claim that the structured path is "proven feasible against Azure AI Mistral by `_provider_safe_response_format`" (#309 is the counter-evidence), and add a line under "When to revisit" recording that rollback condition 3 has fired in dev-test on 2026-08-31, with the config rollback lever named.
- **`docs/architecture/03-components.md`**, "The judge connection" — at most two lines: structured judge calls carry a provider-safe `response_format` whose shape is pinned by the contract test, and provider rejections are diagnosed from a content-free log line.
- **`docs/security-brief.html` — no change.** Line ~302 ("Feedback content, prompts, model output and key values are never written to log messages — enforced in code") remains true because of D2. If you find yourself logging any raw provider text, stop: that is a scope change requiring the brief to be updated and a human to sign it off.

### 6. Verify and open the PR

- `make test` and `make lint` (the latter runs the `import-linter` contracts). `make docs` if any Sphinx-referenced page changed.
- Branch is already `feat/309-loop-bug-analyze-bulk-judge-call-returns-400`, based on `origin/main`. Conventional-commit subjects only, no trailers. Suggested split: one `test:` commit for step 1, one `fix(llm):` for steps 2–3, one `test:` for step 4, one `docs:` for step 5.
- PR body must state plainly, because it is the honest status and the next person needs it:
  1. The root cause is **not confirmed**; this PR ships a diagnostic that names it on the next occurrence plus the highest-likelihood mitigation.
  2. The immediate P1 mitigation available to an operator today is the ADR-020 config rollback (`AZ_JUDGE_LLM_MODEL=""` + re-apply) — a human decision, not taken here.
  3. Post-deploy loop to close: run one analyze-bulk in dev-test and read the new log line. 400 gone → a parameter-level rejection was the cause; record that in ADR-020 and close #309. `rejected_keyword=<kw>` → add `<kw>` to `_UNSUPPORTED_SCHEMA_KEYWORDS` plus a contract-test case; that is the real fix. Still 400 with `rejected_keyword=unknown` → follow the runbook to capture the body and open a follow-up (the leading hypothesis at that point is that the Foundry `/models` route at `api-version=2024-05-01-preview`, set by `var.llm_api_version` and inherited by the judge connection, does not accept a `json_schema` response format at all — which is a config/ADR question, not an adapter one).
  4. `closes #309`.

## Explicitly not in this plan

`$defs`/`$ref` inlining, any change to `AnalyzeJudgeResult` or its constraints, a free-text judge fallback, routing changes for `CodingService` (#310), the `summarize` judge sites (#299), and any Terraform edit. If step 3's mitigation turns out to be the cure, none of these become necessary; if it does not, the diagnostic from step 2 will say which of them to reach for.
