The tree is clean and the spec checks out against the real code. Here is the plan.

---

# Implementation plan — #294: pin single-record summarize output to the detected source language

## Decisions the spec left open

| # | Decision | Which way, and why |
|---|---|---|
| 1 | Helper name | `qfa.services.language.detect_source_language(text: str) -> str \| None`. Deliberately **not** `detect_language` — that name is already taken by `qfa.adapters.presidio_anonymizer.detect_language`, which coarsens to 6 spaCy languages plus `"xx"`. Two same-named helpers with different contracts is the one confusion worth spending a longer name to avoid. |
| 2 | Determinism | Set `DetectorFactory.seed = 0` once at module import. `langdetect` is randomised by default, so without it a borderline record can flip languages between identical requests — and a test asserting `"in en"` becomes flaky. Note in a comment that `DetectorFactory` is process-global, so this also makes the Presidio adapter's `detect_language` deterministic; that adapter's tests use unambiguous strings (`tests/adapters/test_presidio_anonymizer.py:68-87`) and are unaffected. |
| 3 | **Short-text gate (addition beyond the spec's literal wording)** | Return `None` when the stripped text is shorter than 20 characters, via a module constant `_MIN_DETECTION_CHARS = 20`. Measured on this repo's `langdetect` 1.0.9: `"Water is dirty"` → `af`, `"ok"` → `sk`, `"Merci"` → `es` — all wrong, and none raise `LangDetectException`. Pinning a *wrong* language is strictly worse than the bug being fixed, whereas falling back to today's soft instruction is exactly the status quo. At the boundary detection is already good (`"Bedankt voor de hulp"` (20) → `nl`, `"The water is dirty here"` (23) → `en`). This reading is consistent with the spec's AC, which groups "detection failure/**short text**" as one fallback case. |
| 4 | Truncation | Detect on the first 2000 characters only, via `_DETECTION_SAMPLE_CHARS = 2000`. `content` is capped at `max_length=100_000` (`src/qfa/api/schemas.py:386-388`); measured 109 ms for 100k chars vs 1.6 ms for 2k. This is synchronous CPU work on the event loop inside an `async def`, so bounding it matters more than the last 98k characters of evidence do. |
| 5 | ISO code verbatim | As the spec directs: feed `"en"`/`"fr"` straight into `build_output_language_instruction`, no name-mapping table. The model sees `Write the title and summary in en, regardless of...`, which is well within what the shared builder already ships for analyse (`docs/rest-api/index.md:35` already tells callers an ISO 639-1 code is a first-class value there). |
| 6 | Judge call | Not pinned. The summarize judge returns a bare float (`_JUDGE_PROMPT`, `src/qfa/services/summarize.py:69-109`) — there is no free text to get the language wrong. Leave `_build_judge_system_message` alone. |
| 7 | Logging | None. The helper stays pure and total (never raises); a silent fallback to the existing prompt is the documented default, not an anomaly worth a log line per request. |
| 8 | Reuse the shared builder verbatim | Do **not** fork `build_output_language_instruction`'s wording for this path, even though its "including one made inside the analyst's own instruction" clause refers to a prompt the single-record path doesn't have. A harmless trailing clause is cheaper than a second near-identical builder that drifts. |

Two facts worth knowing before you edit tests:

- The default fixture content in `tests/services/test_summarize.py` is `"Some feedback text."` — 19 characters, i.e. *under* the gate in decision 3. So existing tests get no directive at all and stay green either way; **new tests must pass explicit content longer than 20 characters** rather than relying on the default.
- Empty content never reaches `summarize()`: the route short-circuits at `src/qfa/api/routes.py:374-381` (issue #138). The helper still handles empty input, but it's belt-and-braces, not a live path.

---

## Steps

### 1. Add `src/qfa/services/language.py`

New module, services layer, no class. Contents:

- Imports: `from langdetect import DetectorFactory, detect` and `from langdetect.lang_detect_exception import LangDetectException` (same import shape the adapter uses at `src/qfa/adapters/presidio_anonymizer.py:8-9`). Nothing from `qfa.adapters`.
- `DetectorFactory.seed = 0` at module level, with a one-line comment covering decision 2.
- Module constants `_MIN_DETECTION_CHARS = 20` and `_DETECTION_SAMPLE_CHARS = 2000`.
- `def detect_source_language(text: str) -> str | None:` — strip; return `None` if the stripped text is shorter than `_MIN_DETECTION_CHARS`; otherwise `detect()` the first `_DETECTION_SAMPLE_CHARS` characters and return the raw ISO 639-1 code; return `None` on `LangDetectException`.

Docstring: one summary line plus only the facts the signature can't carry — returns an ISO 639-1 code or `None`, never raises, short/undetectable text is deliberately `None` so callers fall back rather than pin a wrong language, and it is *not* the adapter's `detect_language` (which coarsens to Presidio's 6 models). No usage example: `make test` runs `pytest --doctest-modules src`, so any `>>>` block becomes a live test against `langdetect`'s model behaviour, which is not what we want to pin here.

Ruff config in force: `ANN` (annotate the signature), `D` with numpy convention (`D103` applies in `src/`).

### 2. Add `tests/services/test_language.py`

Plain `pytest.mark.parametrize`, no fixtures, matching the flat style of `tests/services/test_prompts.py`. Cases:

| Input | Expect |
|---|---|
| `"The water pump in the camp has been broken for three weeks."` | `"en"` |
| `"Merci beaucoup pour votre aide, la distribution etait bien organisee."` | `"fr"` |
| `""` and `"   "` | `None` |
| `"Water is dirty"` (14 chars) | `None` — the short-text gate, and the direct regression guard on decision 3 (unguarded, `langdetect` answers `af`) |
| `"12345 !!!"` | `None` — the `LangDetectException` path |

Plus two non-parametrised tests:

- Truncation: a long-but-uniform English text well over 2000 characters still returns `"en"` (proves truncation doesn't break detection; a wall-clock assertion would be flaky, so don't add one).
- Determinism: two calls on the same borderline-ish text return equal results (guards the seed).

### 3. Wire detection into `SummarizeService.summarize`

`src/qfa/services/summarize.py`, `summarize()` (currently lines 250-320):

- Add `from qfa.services.language import detect_source_language` to the imports.
- Replace `system_message = _DEFAULT_SUMMARIZATION_PROMPT` (line 276) with a detection on `request.feedback_record.content` — the **raw** record content, before `build_feedback_record_envelope` and before `self._anonymizer.anonymize`, so neither XML envelope tags nor `<PERSON_0>`-style placeholders dilute the sample — followed by an unconditional append of `build_output_language_instruction(detected, subject="title and summary")`. No `if`: the builder already returns `""` for `None` (`src/qfa/services/prompts.py:144-145`), which is what makes the fallback a no-op.
- `build_output_language_instruction` is already imported in this module (line 42) for `summarize_bulk` — do not re-import.
- Leave `_DEFAULT_SUMMARIZATION_PROMPT` (lines 46-54) **unchanged**. Its soft "use the same language as the input" line is precisely the fallback behaviour AC 4 requires when detection returns `None`.
- Add one line to the `summarize()` docstring noting the caller-visible behaviour the signature can't show: the output language is auto-detected from the record content and pinned, with no request field. Nothing else in the docstring changes, and the module docstring doesn't change.

Do not touch `summarize_bulk()` (lines 174-248), `SingleSummaryRequestModel` (`src/qfa/domain/models.py:228-236`), `src/qfa/api/schemas.py`, or `src/qfa/api/routes.py`.

**Ordering: step 1 must land before step 3** (the import).

### 4. Add `TestSingleSummaryDetectedLanguage` to `tests/services/test_summarize.py`

Place it directly after `TestAggregateSummaryOutputLanguage` (which ends at line 386) so the two language stories read together. Use the existing `FakeLLMPort` / `_build_service` / `_make_summary_request(feedback_record=_make_feedback_record(content=...))` / `fake_llm.calls[0]["system_message"]` pattern — no new fixture style, and each test needs a two-response `FakeLLMPort` (`_make_summary_result()` then a float string) because `summarize()` always makes the judge call.

Four tests:

1. **`test_english_record_pins_english_output`** — content: a plain English sentence with no other language cue (e.g. the water-pump sentence). Assert `"Write the title and summary in en" in fake_llm.calls[0]["system_message"]`. Docstring should name this as the regression test for the reported bug (#294: English record, French title/summary). **This is AC 1.**
2. **`test_french_record_pins_french_output`** — a French sentence → `"Write the title and summary in fr"`. Proves the directive tracks the record rather than being hardcoded to English.
3. **`test_short_record_falls_back_to_the_soft_instruction`** — content `"Water is dirty"`. Assert `"Write the title and summary in" not in ...calls[0]["system_message"]`, that the original `"Use the same language as the input feedback item"` line is still present, that no exception is raised, and that a result is still returned with both LLM calls made (`len(fake_llm.calls) == 2`). **This is AC 4 and the second half of AC 6.**
4. **`test_directive_is_absent_from_the_judge_call`** — English content; assert the directive appears in `calls[0]` but not in `calls[1]["system_message"]`. Locks in decision 6.

Do **not** modify `TestAggregateSummaryOutputLanguage` (lines 340-386) — AC 3 requires those tests pass byte-for-byte unchanged.

**Ordering: step 3 must land before step 4.**

### 5. Docs

- `docs/architecture/06-prompt-envelope.md` — extend the existing "Output language directive" section (starts line 30) with a short subsection for the single-record summarize path. Four or five lines, no new prose sprawl: no request field exists and none is added; `detect_source_language` runs on the raw record content before anonymisation; the ISO 639-1 code goes through the *same* builder with `subject="title and summary"`; short or undetectable text yields no directive and the prompt's soft "same language as the input" line stands; `/v1/summarize-bulk` is unchanged because its callers always set `output_language`. **This is AC 7.**
- `docs/rest-api/index.md` — one sentence in the per-record-endpoints paragraph (line 60) stating that `/v1/summarize` needs no language parameter: the output language follows the record's own language automatically. This is a caller-visible behaviour change and AGENTS.md requires the doc update in the same PR; keep it to the single sentence.

No change to `docs/python-api/index.md` — the autosummary is `:recursive:` over `qfa` and picks the new module up on its own. No change to `docs/security-brief.html` — nothing security-relevant changes (the directive lives in the system message, as it already did).

### 6. Verify

- `make test` — the new `tests/services/test_language.py`, the new class in `tests/services/test_summarize.py`, and the untouched `TestAggregateSummaryOutputLanguage`. Also confirm `tests/services/test_orchestrator_judge_routing.py` (it drives `summarize()` but asserts only on routing) and `tests/api/test_routes.py` still pass.
- `make lint` — `ruff`, `ty`, and `lint-imports`. **AC 5 needs no `pyproject.toml` change**: the "Application services depend only on ports, not infrastructure" contract (`pyproject.toml:243-260`) forbids a named list — `openai`, `litellm`, `presidio_*`, `fastapi`, `starlette` — and `langdetect` is not on it, matching the precedent of `hdbscan`/`numpy` already imported by `qfa.services.clustering`. Do **not** add `langdetect` to `forbidden_modules`, and do **not** add an `ignore_imports` entry; both would be wrong signals. The contract that matters — no `qfa.services` → `qfa.adapters` import — is satisfied by construction in step 1.
- `make docs` if the doc edits touch anything structural; the Sphinx build runs with `-W`.

### 7. Commit and PR

Branch `feat/294-loop-bug-output-language-in-different-languag` already exists off `main`. Two conventional commits read cleanly — `feat(services): add source-language detection helper` (steps 1-2) then `fix(summarize): pin single-record summary output to the detected language` (steps 3-5) — but one commit is acceptable. Subject line and optional body only, no trailers. PR into `main` with `closes #294`.

---

## Ordering constraints, condensed

1 → 3 → 4 is a hard chain (helper, then the call site, then the tests that assert on the call site). 2 depends only on 1. 5 is independent and can be written at any point. 6 is last. 7 after 6.

## Environment note

`uv sync` fails in this worktree: `hdbscan==0.8.43` builds from sdist on aarch64 and there is no `cc` on `PATH`. That is pre-existing and unrelated to this change — but it means I could not execute the suite here, and you may hit it before your first `make test`. My `langdetect` measurements above were taken by running the cached `langdetect` 1.0.9 wheel directly against a standalone interpreter, so the thresholds in decisions 3 and 4 rest on this repo's pinned version, not on recollection.
