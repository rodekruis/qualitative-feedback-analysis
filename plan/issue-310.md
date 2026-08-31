## Implementation plan — #310: extend `JUDGE_LLM_*` routing to `CodingService`

### Findings that shape the plan (read first)

**Why #258 excluded `assign_codes` — checked, and it was scope only.** `spec/issue-258.md` (commit `8167b0c`) lists exactly four judge sites under "Notes for the implementer" and its AC line explicitly groups "coding classification" with the *generation* calls that must stay on the primary; its "Out of scope" section says nothing about coding. The `CodingService` extraction (`87e389d`, #265) only carried that exclusion forward into a constructor with no judge seam. ADR-020 (#259) then inherited the same four-site framing. **No latency budget, accuracy concern, or coding-specific reason exists anywhere in history.** So this change is a pure scope widening — it does not overturn a recorded decision, and it needs no new ADR (the mechanism from #258 and the model choice from ADR-020 are both unchanged). ADR-020 gets edited in place instead (step 8).

**Decision left open by the spec — the one-shot pick.** Going with the spec's default: the pick (`coding.py:263`) **stays on the primary**. Reasons to record in the PR body: (a) it is a generation call — it produces the candidate set, it does not grade anything, so moving it would break the "the grader is not the generator" invariant the split exists for; (b) its output contract is materially more fragile than a judge's — a `LLMResponseParseError` there is swallowed and silently degrades to *no codes assigned* (`coding.py:279-289`), so a weaker model on that call site would quietly reduce coding coverage rather than fail loudly; (c) ADR-020 ships with no evaluation evidence, so widening beyond judge calls buys risk with no measurement to back it.

**Two risks to flag, not to fix in this PR** (they belong in ADR-020's *Limitations* / *Rollback conditions*, step 8, and in the PR body — do **not** change behaviour for them here):

1. **The coding judge has no degradation path.** Unlike the two analyze judges (which catch and fall back to `quality_score=None`), `_judge_code_level` raises `AnalysisError` on an out-of-range score → **502 on `/v1/assign-codes`**, and an unparseable structured response propagates as `LLMResponseParseError` → also a failed request. Pointing this site at a cheaper judge model therefore adds a new hard-failure mode to a live endpoint. This is the same theme as #299 (which covers the two summarize sites); fixing it here would be scope creep, but it must be documented.
2. **This call site passes no `timeout`.** `_judge_code_level` calls `complete()` without `timeout=`, so it gets `LiteLLMClient`'s 40 s-per-attempt default rather than a deadline-derived one, unlike every executor-mediated call. Pre-existing, orthogonal, and *not* to be changed here — but the per-attempt budget now applies to a different model than before, so note it.

**Blast radius is small by construction:** `judge_llm` defaults to `llm`, so every existing `CodingService(...)` construction site (`tests/services/test_coding.py:91`, the routing-test builder) keeps compiling and behaving identically. The e2e suite is unaffected: `tests/e2e/conftest.py:174` hands `create_app` an `llm_factory` that returns the *same* `FakeLLMPort` instance for both connections, and no e2e/API fixture sets `JUDGE_LLM_MODEL`.

---

### Step 1 — Add the judge seam to `CodingService.__init__`

**File:** `src/qfa/services/coding.py` (`__init__`, line 175-186; class docstring, 155-174)

- Add `judge_llm: LLMPort | None = None` as the **last** parameter (defaulted, so no existing call site breaks).
- `self._judge_llm = judge_llm if judge_llm is not None else llm`. Copy the inherit-when-unset comment intent from `analyze.py:153-158` — same rule, so the reader who knows one knows all three. Do not re-derive a new fallback shape.
- Rewrite the `llm` param docstring, which currently asserts the exclusion ("There is no second connection: #258 scoped the judge/primary split…"), to: primary serves the one-shot pick; add a `judge_llm` entry matching `analyze.py:118-126` (including the pointer to `resolve_judge_llm_settings`).

**Ordering:** must precede steps 2, 4, 5, 6.

**Test proving it:** new test in `tests/services/test_orchestrator_judge_routing.py::TestDefaultsToThePrimaryClient` — `test_coding_service_judge_client_is_the_primary_client_when_unset`, asserting `service._judge_llm is primary` (identity, matching the existing two).

### Step 2 — Route the per-level judge call

**File:** `src/qfa/services/coding.py`

- **Line 421 only:** `await self._llm.complete(` → `await self._judge_llm.complete(`.
- **Leave line 263 (the pick) on `self._llm`.**
- Everything else in `_judge_code_level` is unchanged: same deadline check, same `check_token_limit`, same anonymisation, no `timeout=` argument added, same 0.0–1.0 range validation. `_judge_selected_path` needs no edit — it issues no LLM call itself.

**Ordering:** after step 1.

**Tests proving it:** step 6.

### Step 3 — Fix the `coding.py` module docstring

**File:** `src/qfa/services/coding.py:23-27`

Replace the final paragraph (which states the exclusion as settled fact and cites the routing test as pinning it) with the new split: the pick runs on the primary connection, the per-level judge runs on `_judge_llm` (the primary when `JUDGE_LLM_MODEL` is unset). Keep it to the two or three lines the old paragraph occupied — per AGENTS.md a docstring must not grow unless behaviour was added. Reference #310.

**Ordering:** independent; same PR.

### Step 4 — Wire it at composition

**File:** `src/qfa/api/composition.py:320-323`

- `coding=CodingService(llm=llm, judge_llm=judge_llm, anonymizer=anonymizer, executor=executor)`.
- Delete the three-line "No judge client: … #258 scoped the split to analyse and summarise" comment above it. If a comment stays, it should say only what isn't obvious: the pick stays on the primary, the per-level judge follows `judge_llm`.
- No change needed in `qfa.api.app`'s lifespan — it already builds and tracks `tracked_judge_llm` and passes it into `build_services`, so `CodingService` picks it up for free. No change needed to `build_services`' `judge_llm` docstring paragraph (already phrased service-agnostically) — re-read it and only touch it if it names the two services.

**Ordering:** after step 1. Step 7's composition test fails until this lands.

### Step 5 — Retarget the routing test's coding builder and module docstring

**File:** `tests/services/test_orchestrator_judge_routing.py`

- `_build_coding_service(primary, judge=None)` (line 218-232): add the parameter, pass `judge_llm=judge`, and **delete the docstring sentence "There is no `judge` parameter on purpose"**. Keep handing the service the real `LLMCallExecutor` built over the *primary*, as the two sibling builders do.
- `RoutingLLM` (line 71-137): add a constructor arg for the pick payload, e.g. `coding_selection: list[int] | None = None` defaulting to `[0]`, so `_payload` returns `CodingResponse(selected=self.coding_selection)`. This is needed for step 6's multi-level case: `flatten_coding_nodes` is pre-order, so index `0` is always a depth-1 root path and would only ever produce one judge call. Extend the class docstring by one clause, not a paragraph.
- Module docstring (lines 1-21): it says "four use-case methods" / "four call sites". Update to five, and say the coding one is the only site that fans out (one judge call per level per selected path). Keep the existing rationale for the module living where it does.

**Ordering:** after step 1, before step 6.

### Step 6 — Flip the routing tests from pinning the exclusion to pinning the inclusion

**File:** `tests/services/test_orchestrator_judge_routing.py`

Delete `TestGenerationCallsStayOnThePrimaryClient::test_coding_classification_stays_entirely_on_the_primary` (line ~460-491) and its exclusion-as-decision docstring. Replace with three tests:

1. **`TestJudgeCallsRouteToTheJudgeClient::test_coding_per_level_judge_calls`** — build a three-level chain framework (`CodingNode(id="l1", children=[CodingNode(id="l2", children=[CodingNode(id="l3")])])`), `RoutingLLM("primary", coding_selection=[2])` so the deepest path is selected, `confidence_threshold=0.5`, judge payload score `0.9` so no early stop. Assert `primary.response_models == [CodingResponse]` and `judge.response_models == [JudgeResponse] * 3`. Also assert the returned `AssignedCodeModel.confidence_level_1..3 == 0.9`, so the test pins that the service *keeps the judge client's verdict* — the same reason the analyze test asserts `quality_score`, not just the call log.
2. **`TestDefaultsToThePrimaryClient::test_coding_judge_uses_the_primary_client_when_unset`** — no judge client, single-level framework: `primary.response_models == [CodingResponse, JudgeResponse]` (this is the exact assertion the deleted test made; it survives, only its meaning changes from "the split excludes coding" to "the default is unchanged"). This is the spec's inherit-when-`JUDGE_LLM_MODEL`-unset case at the behavioural level; step 1's test covers it at the identity level.
3. **`TestGenerationCallsStayOnThePrimaryClient::test_coding_pick_keeps_the_primary_model`** — with a judge configured, `len(primary.calls) == 1` and `primary.response_models == [CodingResponse]`. This is the test that pins the open decision recorded above; its docstring is where the "pick is generation, not judging" reasoning belongs.

Optional but cheap, if it doesn't duplicate an existing assertion: extend `TestCostAccountingAcrossBothClients` with a `TrackingLLMAdapter`-wrapped coding case asserting the `llm_calls` rows come back `["primary-model", "judge-model", …]` — the coding path is now the largest generator of judge rows, so per-model attribution there is worth pinning once.

**Ordering:** after steps 1, 2, 5.

### Step 7 — Invert the composition test, extend the lifespan test

**Files:** `tests/api/test_composition.py:445-460`, `tests/api/test_lifespan.py:185-200`

- `test_coding_service_runs_on_the_primary_connection` currently asserts a configured judge model "does not reach the coding path". Rename (e.g. `test_coding_service_gets_the_judge_connection`) and invert: with `JUDGE_LLM_MODEL` set, keep `services.coding._llm is stub_llm` (generation untouched) and add `services.coding._judge_llm is not stub_llm` plus `services.coding._judge_llm is services.analyze._judge_llm` — one judge client per process, shared, mirroring the existing shared-executor assertions. Rewrite the docstring: it currently cites #258's scoping as the reason.
- Add to the `TestBuildServices` default-path assertions (~line 385-406): with no judge model configured, `services.coding._judge_llm is services.coding._llm`.
- In `test_lifespan.py`'s graph test (line 193-200), alongside `coding._llm is analyze._llm`, add `coding._judge_llm is analyze._judge_llm`. That is the one assertion proving the **tracked** judge client reaches the coding service, i.e. that coding judge calls will be billed — the failure mode #258 called out and the one ADR-020's rollback condition 4 depends on.

**Ordering:** after step 4.

### Step 8 — Documentation (same PR, per AGENTS.md)

1. **`docs/architecture/03-components.md`**
   - Line 43 table cell: `AnalyzeService` and `SummarizeService` "each hold one or two of these" → include `CodingService`.
   - "The judge connection" section (50-72): add `CodingService` to the list of holders; change "Four call sites use the judge connection" to five, listing the coding per-level judge and noting it is the only site whose call count scales with the request (paths × depth); **delete** the two sentences beginning "Everything else … including its per-level judge" / "For the coding path that exclusion is structural", replacing them with the narrowed exclusion (the coding *pick* stays on the generation client).
   - The service table row at 117 already describes the per-level judge accurately; leave it.
2. **`docs/operations/settings-reference.md:21-25`** — replace "It applies to four call sites … The per-level judge inside `assign_codes` stays on the primary connection." with the five-site list. Add one operator-facing sentence: because the coding judge fires once per level per selected path, enabling a judge model shifts a larger share of call volume (and cost) onto the judge connection than the analyze/summarize sites alone. Nothing else in the block changes — no new variable, no new secret, same switch semantics.
3. **`docs/adr/020-mistral-medium-as-judge-model.md`** — status stays **Accepted**; edit in place, no new ADR:
   - *Context* / *Decision*: the "four call sites" count and the enumeration become five, with a short parenthetical that #310 extended the scope to `assign_codes`' per-level judge after this ADR was accepted, and that the #258 exclusion was scope-only (no coding-specific concern was recorded). Note in *Context* that the cost argument strengthens — the coding judge is per level, so more calls move to the cheaper model.
   - *Limitations*: add the two risks from the top of this plan — the coding judge has **no degradation path** (out-of-range score → `AnalysisError` → 502 on `/v1/assign-codes`; unparseable structured output → `LLMResponseParseError` → failed request), unlike the analyze judges that fall back to `quality_score=None`; and this call site runs on `LiteLLMClient`'s default per-attempt timeout rather than a deadline-derived one.
   - *Rollback conditions*: add an observable for the new failure mode — any `AnalysisError` with `"LLM judge returned score outside 0.0-1.0"`, or a sustained rise in 5xx on `/v1/assign-codes` (`infra/observability.tf`).
   - *When to revisit* and the #299 follow-up note need no change.
4. **No change needed** (checked): `docs/architecture/02-system-context.md` (generic "judge calls only"), `06-prompt-envelope.md` (analyze-judge-specific), `docs/rest-api/index.md`, `docs/development/implementing-a-new-endpoint.md`, `docs/security-brief.html` (no security-relevant change — same trust boundary, same credential, no new data leaving the process that wasn't already leaving it), `.env.example`, `infra/`, `tests/scripts/test_infra_judge_config.py`.

### Step 9 — Verify

- `make test` — the whole suite, not just the touched files. Specifically expect green: `tests/services/test_coding.py` (unchanged, proving the default is byte-for-byte the old behaviour), `tests/services/test_orchestrator_judge_routing.py`, `tests/api/test_composition.py`, `tests/api/test_lifespan.py`, `tests/test_settings.py`.
- `make lint` — includes the `import-linter` contracts and `ty`. No layer boundary moves here (`LLMPort` is already imported in `coding.py`), so a contract failure would mean something went wrong.
- Do **not** run the e2e/integration tiers unless a DB is up; they are excluded from the default run and this change does not touch them.

### Ordering summary

1 → 2 → 3 (3 anytime after 2) → 4 → 5 → 6 → 7 → 8 → 9. Hard constraints: 2 needs `_judge_llm` to exist (1); 6 needs the builder parameter and the configurable pick payload (5); 7 fails until composition is wired (4).

### Coverage of the spec's five items

| Spec item | Steps |
|---|---|
| 1. `judge_llm` param, inherit-when-unset | 1 |
| 2. `_judge_code_level` on `self._judge_llm` | 2 |
| 3. Composition wiring | 4 (+7 for the lifespan/tracking assertion) |
| 4. Routing test flipped from exclusion to inclusion, plus the unset-default case | 5, 6 (+7) |
| 5. `coding.py` docstring and ADR-020 updated | 3, 8 |

Out of scope for this PR, deliberately: adding graceful degradation to the coding judge (#299's theme — flagged in ADR-020 *Limitations*; worth a follow-up issue, mention it in the PR body), passing a deadline-derived `timeout` at this call site, and moving the one-shot pick to the judge connection.
