# Implementation plan — #324 (anonymisation placeholder collisions)

## Decisions taken on the points the spec left open

**D1 — The fix goes in the port contract, not in the merge loop.** The collision is a property of the `AnonymizationPort` contract: `anonymize()` opens a *fresh* placeholder namespace per call, so *any* caller that merges two mappings corrupts data. Patching `LLMCallExecutor.anonymize_records` to renumber after the fact would leave that trap armed for the next caller (notably #325, which wants per-record anonymisation in `analyze_bulk`). So: add one new port method, `AnonymizationPort.anonymize_batch(texts) -> (redacted_texts, mapping)`, which anonymises many texts into **one shared placeholder namespace**, and make `anonymize(text)` a one-element delegation to it in `PresidioAnonymizer` so there is a single code path.

**D2 — Shared counter, not `len(mapping)`, and it lives in a call-local object.** Per the spec's preference, numbering comes from an explicit counter. It must **not** become instance state on `PresidioAnonymizer`: `qfa.api.composition.build_services` constructs exactly one anonymiser for the whole process (`composition.py:293`), shared by every service and every request, so instance-level mutable state would leak a placeholder namespace across tenants and grow without bound. The counter is therefore created per `anonymize_batch` call.

**D3 — Numbering stays global across entity types, as today.** Current behaviour derives the index from the whole mapping's size, so a single text already yields e.g. `<PERSON_0>`, `<LOCATION_1>`. Keeping one counter (rather than per-type counters producing `<PERSON_0>`, `<LOCATION_0>`) is the smaller behavioural change and needs one integer to guarantee uniqueness. Placeholder *format* is unchanged, so `AnalyzeService._is_retained_analyze_placeholder` (prefix match on `<TYPE_`) and the `<PERSON_0>`-style examples in prompts keep working.

**D4 — Nothing depends on per-record renumbering** (the spec asked us to check). Every occurrence of a literal `<PERSON_0>` / `<LOCATION_0>` in `src/`, `tests/`, `fixtures/` and `docs/` is either prose in a prompt or docstring (`sensitivity.py:40-41`, `summarize.py:284`, `analyze.py:212`) or a hand-written fake anonymiser's own invented mapping (`tests/services/test_llm_call_executor.py:57`, `test_analyze_hierarchical.py:62`, `test_analyze.py:596/626/674`, `test_summarize.py:285`). No fixture or assertion pins a real Presidio index. No prompt text promises numbering restarts.

**D5 — The analyst prompt joins the same namespace.** `analyze_hierarchical` today anonymises the prompt in a *second* `anonymize()` call and merges it (`analyze.py:399-402`) — the identical collision, one line below the one in the issue. Fixing only the record loop would leave AC "every placeholder maps to exactly one original value" false end-to-end. So the executor's batch entry point takes the records **and** the analyst prompt, and `analyze.py`'s `{**mapping, **prompt_map}` merge disappears.

**D6 — `analyze_bulk`'s prompt/message split gets the same treatment (step 8, deliberately last).** `analyze.py:262-265` anonymises the assembled user message and the prompt in two calls and *discards* the prompt's mapping. Same bug class: `<LOCATION_0>` in the prompt and `<LOCATION_0>` in the records mean different things to the model, and step-7 de-anonymisation restores prompt placeholders using the message's mapping. Given `anonymize_batch` this is a two-line fix, so it belongs in this PR. It is sequenced last and is independent of steps 1–7 — if it turns into a test-churn hole, drop it and open a follow-up rather than unpicking anything earlier.

**D7 — Do not "fix" `deanonymize`'s substring replace.** With cross-record numbering, double-digit indices become routine, so `<PERSON_1>` and `<PERSON_10>` now coexist. The trailing `>` makes `str.replace("<PERSON_1>", …)` non-matching inside `<PERSON_10>`, so plain iteration is already safe. Step 3 adds a test that pins this invariant instead of changing the code.

**D8 — Preserve the `original_value == "PII"` early return verbatim**, including the fact that `<PII>` is not registered in the mapping. Undocumented, untested, out of scope.

## Steps

### 1. Declare the batch method on the port

`src/qfa/domain/ports.py`, `AnonymizationPort`.

- Add `def anonymize_batch(self, texts: tuple[str, ...]) -> tuple[tuple[str, ...], dict[str, str]]: ...` — tuple in/tuple out, matching `EmbeddingPort.embed`'s house style (`ports.py:42`).
- Docstring must state the three contract facts that are the actual fix, because they are what stops the bug regressing:
  1. Placeholders are unique across **all** `texts` in one call, and the returned texts are in input order.
  2. The same original value in two different texts gets the **same** placeholder.
  3. Each call opens an independent namespace — **mappings from separate calls must not be merged.** Callers that need one namespace across several texts must use this method.
- Amend the class docstring's "deterministic … within a single call" sentence to name `anonymize_batch` as the multi-text form.

No import-linter contract changes (no new module); no ADR (this refines an existing port, it does not decide a new structure).

### 2. Implement it in the adapter

`src/qfa/adapters/presidio_anonymizer.py`.

- Add a module-private mutable value object holding the namespace, so mapping and counter cannot drift apart:
  ```
  @dataclass
  class _PlaceholderSpace:
      mapping: dict[str, str]        # placeholder -> original
      _by_value: dict[tuple[str, str], str]   # (entity_type, original) -> placeholder
      _next_index: int = 0
      def placeholder_for(self, original_value: str, entity_type: str) -> str: ...
  ```
  `placeholder_for` replaces the static `_get_unique_id`: keep the `"PII"` early return (D8), look up `(entity_type, original_value)` in `_by_value` for the reuse case, else mint `f"<{entity_type}_{self._next_index}>"`, increment, and record in both dicts.
- **The reverse index is not a micro-optimisation — it is required.** `_get_unique_id`'s current dedup is a linear scan of the mapping, which was cheap only because the mapping was one record long. With a request-wide namespace (`analyze_hierarchical` routinely runs thousands of records) the scan becomes O(entities²) over the whole corpus and would show up as anonymisation latency, which `analyze.py:404-407` logs as its own phase.
- Split the current `anonymize` body into `_anonymize_into(self, text: str, space: _PlaceholderSpace) -> str` — everything as it stands (language detect, analyze, per-entity `OperatorConfig` with the `ent=entity` default-arg closure, the `DATE_TIME` `"keep"` operator) except that it takes the space instead of creating a mapping, and the lambda calls `space.placeholder_for(x, ent)`.
- `anonymize_batch(texts)`: create one `_PlaceholderSpace`, map `_anonymize_into` over `texts` in order, return `(tuple(redacted), dict(space.mapping))` — return a copy so the caller cannot mutate the space.
- `anonymize(text)`: delegate — `(redacted,), mapping = self.anonymize_batch((text,))`. Behaviour for a single text is unchanged.
- Update the `PresidioAnonymizer` class docstring: "the same value gets the same placeholder within a single `anonymize` call" becomes "within a single `anonymize`/`anonymize_batch` call — across every text in the batch."

Note for whoever writes assertions here: Presidio applies operators in reverse document order, so within one text the *later* entity gets the *lower* index (this is why the issue's repro shows `<ORGANIZATION_1>` for the leading name and `<LOCATION_0>` for the trailing place). Assert on uniqueness and round-trip, never on which index a given entity received.

### 3. Adapter tests (real Presidio)

`tests/adapters/test_presidio_anonymizer.py` — runs by default (no marker), and `pyproject.toml` pins the spaCy models as dependencies, so real-Presidio assertions are available here.

- `test_batch_gives_distinct_records_distinct_placeholders` — **the AC-4 regression test**, built from the issue's repro verbatim: `("Olena Kovalenko reported the water point in Kharkiv ran dry.", "Piet Jansen reported the water point in Utrecht ran dry.")` through one `anonymize_batch`. Assert (a) `len(set(mapping.values())) == len(mapping)` — no placeholder is shared by two originals; (b) `"Kharkiv"` and `"Utrecht"` are both present as values; (c) `deanonymize(redacted[0], mapping) == texts[0]` and likewise for `[1]`. Assertion (c) is the one that fails today.
- `test_repeated_value_across_texts_reuses_one_placeholder` — the same person/location in two texts; assert the two redacted texts contain the same placeholder for it and the mapping has one entry for that value.
- `test_deanonymize_is_unambiguous_with_double_digit_indices` — pins D7. Batch enough distinct entities to push indices past 9 (≥12 texts, one distinct location or name each), then assert every text round-trips exactly. This is the test that would catch a future prefix-collision refactor of `deanonymize`.
- `test_anonymize_matches_a_single_text_batch` — `anonymize(t)` equals `anonymize_batch((t,))` unpacked, pinning the single delegation path.
- Existing tests in this file must pass untouched (single-text round-trip, `DATE_TIME` preserved, empty string, undetectable language).

Ordering: steps 1–3 are self-contained and land the adapter-level fix. Do them first; the rest is plumbing that consumes it.

### 4. Executor: one batch entry point, no merge

`src/qfa/services/llm_call_executor.py`.

- Replace `anonymize_records(records, anonymize)` with:
  ```
  def anonymize_records_and_prompt(
      self,
      records: tuple[FeedbackRecordModel, ...],
      analyst_prompt: str,
      anonymize: bool,
  ) -> tuple[tuple[FeedbackRecordModel, ...], str, dict[str, str]]
  ```
  The rename is the point: a method called `anonymize_records` that also redacts the prompt would be misnamed, and the two *must* be one call (D5). There is exactly one production caller (`analyze.py:391`), so the rename is cheap; do not keep a deprecated alias.
- Body: when `anonymize` is False return `(records, analyst_prompt, {})` — keep returning the *same* records object so the existing `assert anonymized is records` identity check still holds. Otherwise one call:
  `redacted, mapping = self._anonymizer.anonymize_batch((analyst_prompt, *(r.content for r in records)))`, then `redacted[0]` is the prompt and `redacted[1:]` pairs with `records` for `model_copy(update={"content": …})`. The `merged.update(mapping)` loop at line 123 — the bug — is deleted.
- Docstring: keep the "metadata is left untouched (codes/dates are not PII and feed the deterministic trend table)" fact; add why the prompt travels with the records (one shared placeholder namespace — a separately-anonymised prompt cannot be merged in without collisions).
- Leave `anonymize_text` and `deanonymize_json` alone. `_json_escape_mapping` is unaffected.

### 5. Update the test doubles

Eight hand-written `AnonymizationPort` implementations exist and each needs `anonymize_batch`, because the port now declares it and this repo's rule is that doubles inherit the port explicitly (AGENTS.md):

- `tests/services/test_summarize.py:156` `FakeAnonymizer` (no-op → `return texts, {}`) — the widely reused one; also the inline double at `test_summarize.py:285`.
- `tests/services/test_llm_call_executor.py:47` `RedactingAnonymizer`.
- `tests/services/test_analyze_hierarchical.py:53` `RecordingAnonymizer` — must keep appending every text it sees, since `test_anonymization_happens_before_any_llm_or_embed_call` (line 248) asserts on `anonymized_texts`.
- `tests/services/test_orchestrator_judge_routing.py:170` `NoopAnonymizer`.
- `tests/services/test_analyze.py:595` `DeanonymisingFakeAnonymizer`, `:625` `FakeAnonymizerWithPlaceholders`, `:673` `PromptAnonymizer`.

For the redacting/recording doubles, implement `anonymize_batch` as a loop over their own `anonymize` merging the (single-key, collision-free) mappings — 3 lines each. Do **not** invent a second shared-namespace fake: real-namespace behaviour is verified in step 3 against Presidio. Also check for `MagicMock(spec=AnonymizationPort)` uses while here (none found, but `spec=` mocks pick the new method up automatically anyway).

Ordering: step 5 must land with step 4 (or before it) — `test_analyze_hierarchical.py` goes red the moment `analyze_hierarchical` calls the batch path against a double lacking the method.

### 6. Rewire `analyze_hierarchical`

`src/qfa/services/analyze.py`, step 1 of the flow (lines 386-407).

- Replace the `anonymize_records` call, the `if anonymize:` prompt block, and the `mapping = {**mapping, **prompt_map}` merge with the single call:
  `anonymized_records, anonymized_prompt, mapping = self._executor.anonymize_records_and_prompt(request.feedback_records, request.prompt, anonymize)`.
- The "Single pass over the prompt…" comment at lines 396-398 describes machinery that no longer exists — delete it; the reason the prompt is in the same call now belongs on the executor method (step 4).
- Everything downstream is untouched: the trend table still reads `request.feedback_records` (originals), `texts` still comes from `anonymized_records`, and step 7's `restorable` filter plus `_is_retained_analyze_placeholder` still work because the placeholder format is unchanged. Keep the `anonymize_sw` timing block around the new call so the phase-breakdown log line stays honest.

### 7. Hierarchical end-to-end test — AC 5

`tests/services/test_analyze_hierarchical.py`, one new test wired with the **real** `PresidioAnonymizer` (the only double in the graph being `FakeEmbeddingPort` and the LLM):

- Corpus: ~6 records with **distinct** locations across records (Kharkiv / Utrecht / Lviv …) and **one repeated** person name in two records, on two clusterable themes so the existing `FakeEmbeddingPort` keyword vectors split them.
- Fake LLM: instead of `RecordingLLM`'s canned `"PARTIAL_OR_REDUCE"`, echo the received `user_message` back as the partial/synthesis (keep `_is_judge_call` dispatch returning `_judge_text()` so confidence still computes). This makes the de-anonymisation round-trip observable in `result.result` — the deliverable end of the pipeline.
- Assertions: (a) every original location appears in `result.result` attached to the same surrounding text it came from — i.e. no record's location was rewritten to another record's; (b) no raw location string appears in any `user_message` the LLM received; (c) the repeated person yields one placeholder, which per `_ANALYZE_RETAINED_PLACEHOLDER_TYPES` is still `<PERSON_…>` in the output (so assert the *name* is absent and the placeholder present — do not assert PERSON is restored). Assertion (a) is what fails on `main`.
- Name it for the invariant, e.g. `test_hierarchical_round_trips_distinct_entities_across_records`, and note in the docstring that it deliberately uses the real anonymiser because the collision only exists in the real placeholder allocator.

Ordering: step 7 depends on step 6.

### 8. `analyze_bulk`: message and prompt in one namespace (D6 — last, droppable)

`src/qfa/services/analyze.py:258-266`.

- Replace the two `self._anonymizer.anonymize(...)` calls with one
  `(anonymized_user_message, anonymized_prompt), anonymization_mapping = self._anonymizer.anonymize_batch((user_message, request.prompt))`.
- Behaviour change to state in the PR body: prompt-derived placeholders are now in `anonymization_mapping`, so the prompt's redacted entities are restored in the output (subject to the same retained-`PERSON` filter) instead of being discarded and/or wrongly restored from the message's namespace. That is the correct behaviour, and it is what AC 1 means for this path.
- Check these existing tests still pass and update the doubles they define (step 5 already covers the classes): `test_analyze.py:673` `PromptAnonymizer` (asserts the anonymised prompt reaches `judge_system`), `:595` and `:625` (the retained-`PERSON` / restored-`LOCATION`,`EMAIL` assertions). If any of them assert on the *number* of `anonymize` calls, that assertion is now wrong and should become an assertion about one `anonymize_batch` call.

### 9. Documentation, in the same PR

- `docs/architecture/03-components.md:44` — port row: add `anonymize_batch(texts) -> (texts, mapping)` and state that placeholders are unique across the batch.
- `docs/architecture/03-components.md:155` — executor method table: `anonymize_records(records, anonymize)` → `anonymize_records_and_prompt(records, analyst_prompt, anonymize)`, described as "redact every record's text *and* the analyst prompt in one shared placeholder namespace, returning new records, the redacted prompt, and the restore mapping".
- `docs/architecture/04-crosscutting.md:29` — the bullet currently reads "one call per record, mappings merged", which after this change is both wrong and the description of the bug. Rewrite as one batch call, one namespace, no merging; add the caller-facing rule that mappings from separate `anonymize` calls must never be merged.
- `docs/architecture/07-hierarchical-analysis.md:45` and the sequence diagram at :187-188 — records and prompt are anonymised in a single batch call.
- `docs/architecture/06-prompt-envelope.md:191-194` — only if step 8 lands: collapse the two `anonymize` arrows into one `anonymize_batch(user_message, prompt)`.
- `docs/security-brief.html` — **no change.** Its only relevant claim (line 281: "every AI call is wrapped anonymise → send → de-anonymise; the model never sees raw personal data") is still exactly true; this fix changes placeholder allocation, not what is sent. Do not pad it.
- Per the docs style rule: none of these pages gets longer except where a genuinely new fact (the batch method, the no-merging rule) is added.

## Verification

- `make test` and `make lint` must both pass (`lint` runs ruff + `ty` + `lint-imports`; `ANN` and `D` are enabled, so the new port method, `_PlaceholderSpace` and `placeholder_for` all need annotations and docstrings). `make test` also runs `pytest --doctest-modules src`, so don't put an unverified example in a new docstring.
- Targeted while iterating: `uv run pytest tests/adapters/test_presidio_anonymizer.py tests/services/test_llm_call_executor.py tests/services/test_analyze_hierarchical.py tests/services/test_analyze.py`.
- Sanity-check the AC-1 claim against real Presidio once outside the suite by running the issue's repro through `anonymize_batch` and confirming `len(set(mapping.values())) == len(mapping)` and that both records round-trip.
- Note: this planning worktree cannot build `hdbscan` (no C compiler present), so no test was executed while writing this plan. The builder should expect to run `make sync` first and should treat a build failure there as an environment problem, not a code one.

## Out of scope

- Adopting per-record anonymisation in `analyze_bulk` (#325) — this PR only removes the blocker.
- Any change to `deanonymize`'s substring-replace strategy (D7), the `"PII"` special case (D8), or the retained-`PERSON` policy.
- Consolidating the eight near-duplicate anonymiser test doubles into one shared module. Tempting while touching all eight, but it is a separate refactor and would bury this fix's diff.

## Commit shape

Conventional commits, subject line and optional body only, no trailers. Suggested split: (1) `fix(anonymization): allocate placeholders in one namespace per batch` — steps 1-3; (2) `fix(analyze): anonymise records and prompt in one placeholder namespace` — steps 4-7; (3) step 8 if it lands; (4) `docs: …` for step 9 if not folded into the code commits. PR body closes #324.
