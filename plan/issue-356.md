BLOCKED? No — the foundations the spec builds on (#391's pool scripts, #392's harness) are all present in this tree. Two things the spec names are not, and I resolve both below rather than inventing a foundation. Here is the plan.

---

# Implementation plan — #356, Spanish in the analyze eval pool

## What I checked first

The spec rests on `eval/upload_pool.py`, `eval/make_analyze_cases.py`, `eval/analyze_eval.py`, `eval/analyze_scorers.py`, `eval/pool_spec.py` and `scripts/check_pool_masking.py`. All exist and all are tested under `tests/eval/`. `langdetect` is already a project dependency (`pyproject.toml`), so the harness can use it without a new package. `es_core_news_sm` is already installed, so the anonymizer can handle Spanish text.

Three things that would otherwise cost the builder time, verified so they need no work:

- `analyze_scorers._CITED_ID` is `\b[a-z]{2}-\d{4}\b`. It already matches `es-0042`.
- `qfa.services.record_links.format_record_citations` is language-safe by design: the citation label is not checked (`Registros:` counts) and `OVERFLOW_HEADING` is English in every output language (`src/qfa/services/record_links.py:26`). A Spanish answer scores the same way an English one does.
- `output_language: "Spanish"` is a supported value (`_LANGUAGE_ALIASES` in `src/qfa/api/schemas.py`).

## Decisions the spec left open

**D1 — Spec task 2 (the keyword vocabulary) is dropped.** There is no vocabulary in this tree. `cac0ae8 refactor(eval): drop the keyword vocabulary from the eval` removed it, and the answer key is now computed from record ids and labels only. There are no "vocabulary checks" to run again. The checks that take its place are the existing `PLANTED` counts in `validate_pool` (which hold for Spanish, because the labels are copied) plus the new cross-language check in step 5. Do not re-add a vocabulary. Two stale mentions get fixed as part of this work: the phrase "or too few keywords" in `validate_pool`'s docstring, and `feedback/vocab-v1` in ADR-023.

**D2 — Acceptance criterion 2 is read as: matched by `base_id`, the two answer keys are identical apart from the record ids.** The spec says "only the keywords are different", which was true of the old design. Under the current one, keywords do not enter the key at all; the record ids do. Step 5 asserts exactly that, after mapping `es-NNNN` → `base_id`.

**D3 — `judge_failed` does not exist yet, so step 6 adds it.** Today a failed judge shows as four missing scores (`judge_scores` skips a null). You cannot compare "missing" between two runs. It becomes a numeric 1/0 item score, recorded only when the outcome is `ok`.

**D4 — Language is an explicit `--language` flag, not derived from the input file or the record metadata.** A typo in a YAML filename would otherwise upload a Spanish pool over the English dataset. The flag has `choices`, so a wrong value fails in argparse.

**D5 — `base_id` is required only on a non-`en` pool.** The alternative — adding `base_id: null` to `EXPECTED_METADATA_KEYS` — would rewrite all ~110 English items to add a null field. English records are left untouched.

**D6 — The cross-language check lives in `make_analyze_cases.py`, not `upload_pool.py`.** `upload_pool.py` stays a single-file checker that reads no other dataset. `make_analyze_cases.py` already reads the pool from Langfuse, and it is the point where a mismatch would actually do damage (a wrong answer key). It aborts before anything is written, so a failure there costs only a re-upload of the pool.

**D7 — Spanish ids mirror their English number: `en-0042` → `es-0042`, and `created` is copied verbatim.** The spec only says "a new id". Mirroring makes review and debugging far easier, leaks no more label signal than the English ids already do (they came from a seeded shuffle), and copying `created` makes the send order identical in both languages — so the two runs differ by language and nothing else. `base_id` remains the authoritative link and is validated; nothing in the code derives the link from the number.

**D8 — The Spanish case set is not added to the `evaluate` umbrella workflow.** `.github/workflows/evaluate-analyze.yaml` already takes a `dataset` input, so a Spanish run needs no CI change — dispatch it with `analyze/cases-frequent-es-v1`. Adding it to `evaluate.yaml` would double the cost and the 130-minute job time of every full eval run, for a case set that the spec wants run three times by hand.

**D9 — `docs/operations/langfuse-guide.html` is not updated.** AGENTS.md asks for it when what we send to Langfuse changes. That page describes categories ("a dataset is a fixed set of test feedback with the correct answers"), never individual datasets or score names, so a new dataset and two new scores do not change a sentence in it. `docs/security-brief.html` is likewise untouched — nothing security-related changes.

**D10 — Spanish banned words are added.** `BANNED_WORDS` is four English terms (`PSEA`, `safeguarding`, `exploitation`, `referral`). None of them can appear in Spanish prose, so the check would be silently vacuous for the new pool. Step 2 makes it per-language. Keep the Spanish list to direct translations of the same four; do not widen it.

**D11 — The "is this record still in English?" check is a printed warning, never a `SystemExit`.** It catches an untranslated record, but `langdetect` can misfire on a short record with a proper noun, and a blocked upload with no override is worse than a warning. The scored version of this question is `language_match` in the harness.

---

## Part A — code and docs (lands in the PR)

Steps 1–3 are ordered: both later scripts import the constants from step 1. Steps 6–7 are ordered against each other. Step 8 is independent of 1–7. Step 9 comes last.

### Step 1 — per-language names and label keys in `eval/pool_spec.py`

Replace the two singular constants and add two more:

- `RECORDS_DATASETS = {"en": "feedback/records-en-v1", "es": "feedback/records-es-v1"}` (replaces `RECORDS_DATASET`).
- `FREQUENT_CASES_DATASETS = {"en": "analyze/cases-frequent-en-v1", "es": "analyze/cases-frequent-es-v1"}` (replaces `FREQUENT_CASES_DATASET`).
- `BANNED_WORDS` becomes `dict[str, tuple[str, ...]]`, keyed by language (D10).
- `LABEL_KEYS`: every label a family reads, plus `groups`. Derive it from `FAMILY_LABEL` rather than writing a second list, so a new family cannot leave it stale.

`PROMPTS_DATASET` stays singular — one dataset holds both languages.

Update the module docstring to say what is keyed by language and why.

**Tests:** `tests/eval/test_make_analyze_cases.py` imports `RECORDS_DATASET` and must move to `RECORDS_DATASETS["en"]`. Add `tests/eval/test_pool_spec.py` with one test: `LABEL_KEYS` covers every value in `FAMILY_LABEL` and `"groups"`.

### Step 2 — `--language` and `base_id` in `eval/upload_pool.py`

- Add `--language`, `choices=sorted(RECORDS_DATASETS)`, default `"en"`. The existing English invocation keeps working unchanged.
- `ID_PATTERN` becomes a function of the language: `re.compile(rf"^{language}-\d{{4}}$")`. The error message follows (`Ids not shaped es-NNNN`).
- `validate_pool(..., language: str = "en")`:
  - every record's `metadata["language"]` equals `language`;
  - when `language != "en"`: every record has a non-null `base_id`, each `base_id` matches `^en-\d{4}$`, and no two records share one;
  - when `language == "en"`: no `base_id` check at all (D5);
  - `BANNED_WORDS[language]` replaces `BANNED_WORDS`;
  - a printed warning (not an exit) naming any record whose text does not detect as `language` via `langdetect` (D11). Set `DetectorFactory.seed = 0` at module level.
  - Fix the stale "or too few keywords" in the `Raises` section, and document the new rules there. The docstring must not get longer than the behaviour added.
- `main()` uploads to `RECORDS_DATASETS[args.language]`.

The `PLANTED`, `DECOYS`, window, length, `facts` and `review_status` checks are unchanged and apply to the Spanish pool as they stand. The existing `final=True` rule — urgent, decoy and group records must be `review_status: reviewed` — is what enforces spec task 3 (the native-speaker review) before a real upload. No new code for that.

**Tests** in `tests/eval/test_upload_pool.py` (the `_record`/`_metadata` helpers take a language argument): an `es` pool with correct ids and `base_id`s passes; an `en-` id under `--language es` exits; a missing `base_id` exits naming the record; a `base_id` not shaped `en-NNNN` exits; two records sharing a `base_id` exit; `metadata["language"]` not matching exits; a Spanish banned word in Spanish content exits; and a regression test that an `en` record with no `base_id` still passes.

### Step 3 — `--language` in `eval/make_analyze_cases.py`

- `OUTPUT_LANGUAGE` gains `"es": "Spanish"`.
- `--language`, `choices=sorted(RECORDS_DATASETS)`, default `"en"`.
- `build_cases(records, prompts, read_at, language="en")`: the hardcoded `!= "en"` filter uses the argument; `records_dataset` in each case's metadata is `RECORDS_DATASETS[language]`; the case id is already `f"{prompt_id}-{language}"` and needs no change.
- Two new `SystemExit`s in `build_cases`, both before any upload: no prompt matches `language`; and the prompt ids for `language` are not the same set as for `en` (so a half-translated prompt set cannot produce a short case set that looks complete).
- `main()` reads `RECORDS_DATASETS[args.language]` and uploads to `FREQUENT_CASES_DATASETS[args.language]`.

**Tests** in `tests/eval/test_make_analyze_cases.py`: `build_cases(..., language="es")` returns one case per Spanish prompt with id `P01-es`, `output_language` `"Spanish"` and `records_dataset` the Spanish pool; a language with no prompt exits; a Spanish prompt set missing one `prompt_id` exits. The existing English test is unchanged apart from the constant rename.

### Step 4 — `check_translation()` in `eval/make_analyze_cases.py`

A pure function next to `build_answer_key`:

```
check_translation(records, base_records) -> None
```

It raises `SystemExit` on the first problem, naming the record:

1. **Link (AC1).** Every record has a `base_id`; each names a record in `base_records`; no two share one; and every base record is named exactly once. Both directions, so a missing translation is caught too.
2. **Labels (AC1).** For every key in `LABEL_KEYS`, the record's value equals its base record's value. `groups` compares as a set. The message names the record, the label, and both values — this is the readable error a translator will actually see.
3. **Answer keys (AC2).** For every family in `FAMILY_LABEL`, `build_answer_key(records, family)` with its ids remapped `es → base_id` equals `build_answer_key(base_records, family)`. A small `_relabel_key(key, base_by_id)` helper rewrites `labels` keys, `groups` values and `urgent`, re-sorting each. Check 3 is implied by check 2, but it is the acceptance criterion itself and it is cheap, so assert it directly as a backstop.

`main()` calls it when `args.language != "en"`, after fetching `RECORDS_DATASETS["en"]` at the same `read_at` as the translated pool, and before `build_cases`. Print one line on success (`checked N records against feedback/records-en-v1`).

**Tests** in `tests/eval/test_make_analyze_cases.py`, each with a 3–4 record pair: happy path returns None; a missing `base_id` exits naming the record; a `base_id` naming an unknown English id exits; two records sharing a `base_id` exit; an English record with no translation exits; a changed `theme` exits naming the record and the label; a changed `groups` list exits. Add one test that `_relabel_key` on a hand-made key maps `labels`, `groups` and `urgent`.

### Step 5 — `language_match` in `eval/analyze_scorers.py`

Module level: `from langdetect import DetectorFactory, detect_langs` (and `LangDetectException`), `DetectorFactory.seed = 0` with a one-line comment on why the seed is pinned — `eval/` must not import `qfa`, so it cannot reuse `qfa.services.language` (ADR-023), and an unseeded detector gives a different score for the same answer.

```
language_match(text, expected) -> Evaluation
```

- Value `1.0` when the detected ISO code equals `expected`, else `0.0`. Numeric, not categorical, so Langfuse can average it over a run — a run's `language_match` is then the share of cases answered in the right language.
- `comment` is `f"{detected}, expected {expected}"`. `analyze_eval._raw_counts_line` prints the part before the first comma, so the console shows `P03-es language_match es`.
- `metadata={"detected": ..., "expected": ...}`.
- A `LangDetectException` scores `0.0` with `detected` as `"unknown"`. It never raises.

**Tests** in `tests/eval/test_analyze_scorers.py`: a Spanish answer against `"es"` scores 1.0; the same answer against `"en"` scores 0.0 and the comment names `es`; an English answer against `"es"` scores 0.0; text the detector cannot read (`"12345 !!!"`) scores 0.0 with `detected == "unknown"`; two calls on the same text give the same result (the seed is pinned).

### Step 6 — `judge_failed` in `eval/analyze_scorers.py`

In `judge_scores(output)`, append `Evaluation(name="judge_failed", value=1.0 if output.get("quality_score") is None else 0.0)`. The four existing scores keep being omitted when null — a missing score and an explicit failure flag say different things and both are wanted. Update the docstring to say so.

**Tests** in `tests/eval/test_analyze_scorers.py`: a full judge response yields the four scores plus `judge_failed == 0`; a response with `quality_score: None` yields only `judge_failed == 1`.

### Step 7 — wire both scores into `eval/analyze_eval.py`

Depends on steps 5 and 6.

- `_judge_scores_evaluator` gains the guard `if output.get("outcome") != "ok": return []`. Without it a timed-out case would report `judge_failed = 1`, which is false — no judge ran. Behaviour for `ok` cases is unchanged.
- New `_language_match_evaluator(*, output, metadata, **kwargs)`: returns `[]` unless the outcome is `ok`, else `[language_match(output["analysis"], metadata["language"])]`. The Langfuse SDK passes the dataset item's own metadata to item evaluators as `metadata` — verified in the installed SDK at `langfuse/_client/client.py:3092` and `:3109`. Use `metadata["language"]` (the ISO code `es`), not `input["output_language"]` (the English name `Spanish`), so it compares directly with what `langdetect` returns.
- Add it to the `evaluators=with_score_metadata([...])` list in `main()`.

Nothing else in this file needs to change: `--dataset` is already generic, `_case_label` already reads `metadata["language"]`, and `load_records` already fetches whatever `records_dataset` each case names.

**Tests** in `tests/eval/test_analyze_eval.py`: `_language_match_evaluator` returns `[]` for a non-`ok` output; it returns one `language_match` Evaluation reading `metadata["language"]` for an `ok` one; `_judge_scores_evaluator` returns `[]` for a non-`ok` output even when the body carries judge fields.

### Step 8 — `--language` in `scripts/check_pool_masking.py`

Independent of steps 1–7. The working prompts YAML holds both languages, and for a Spanish pool you want the Spanish prompts anonymised in the same batch — that is the shape the service actually sends.

- Add `--language` (default: no filter, current behaviour).
- A pure `_select_prompts(prompts, language)` filtering on `metadata["language"]`, so it is testable without importing `qfa`. Exit with a clear message when the filter leaves no prompt.
- Keep the `qfa` import inside `main()`, as it is now (ADR-023).

**Tests:** new `tests/scripts/test_check_pool_masking.py` covering `_select_prompts` — no language returns everything; `"es"` returns only the Spanish prompts; a language with no prompt exits. Note that `make ruff` formats `src tests eval` only, not `scripts/`, so run `uv run ruff format scripts/check_pool_masking.py` by hand.

### Step 9 — docs

In the same PR, per AGENTS.md.

- **`eval/README.md`.** The table rows for `upload_pool.py` and `make_analyze_cases.py` say "always uploads to `feedback/records-en-v1`" / `analyze/cases-frequent-en-v1` — both become "the dataset for `--language`". Document the new flag on both, the `base_id` metadata field in the record example, the cross-language check, and the Spanish case example. Add `judge_failed` and `language_match` rows to the `analyze_eval.py` score table. Add a short **Spanish pool** runbook section (the Part B steps below). One sentence in "Comparing versions" reminding the reader that two new scores need two new dashboard widgets added by hand — the page already says a new dataset or script does.
- **`scripts/README.md`.** One clause on `--language` in the `check_pool_masking.py` row.
- **`docs/adr/023-eval-black-box-and-langfuse-ground-truth.md`.** One line in Consequences: the keyword vocabulary and `feedback/vocab-v1` were dropped (`cac0ae8`); the ground truth is the labels alone. Do not rewrite the Decision — the ADR records what was decided at the time.

Keep every edit at or below the length of what it replaces unless behaviour was added. The README is long already.

---

## Part B — data and operational steps (the ticket's other half)

These produce domain data in `.corpus_work/`, which git ignores, and spend real money. A headless build agent should do as many as its credentials allow and **report plainly which it could not do**. Do not fake a result. Steps 12 and 13 cannot be done by an agent at all.

10. **Rebuild `records-en-v1.yaml` locally.** The English working file is not in the repo. Reconstruct it from Langfuse with the existing `make_analyze_cases.record_from_item`, which is the exact inverse of `upload_pool.build_items`. Put the snippet in the README runbook rather than adding a script for it.

11. **Translate into `.corpus_work/analyze-pool/records-es-v1.yaml`.** Per record: id `es-NNNN` mirroring the English number (D7), `base_id` set to the English id, `language: es`, `created` copied verbatim, every label copied unchanged, `facts` translated, `gen_model`/`gen_date` set to the translating model and date, `review_status: not_reviewed`. Then `uv run python eval/upload_pool.py --language es --input .corpus_work/analyze-pool/records-es-v1.yaml --dry-run` until it is clean. The dry run also prints the step-2 language warnings.

12. **Native-speaker review** (spec task 3), urgent and group records first. Flip those to `review_status: reviewed`. A real upload is blocked until they are — that check already exists.

13. **Masking check** (spec task 4): `uv run python scripts/check_pool_masking.py --input .corpus_work/analyze-pool/records-es-v1.yaml --prompts .corpus_work/analyze-pool/prompts-v1.yaml --language es`. Reword any record whose planted word gets masked, then re-run step 11's dry run. Expect a different result from English: the Spanish pipeline has no NRP recogniser, so nationality and religion words that English masks will come through.

14. **Upload** the pool (`--language es`, no `--dry-run`), then build the case set: `uv run python eval/make_analyze_cases.py --language es --dry-run`, then for real. The dry run is where `check_translation` fails if the translation drifted — AC1 and AC2 are proven here, on the live data, not in the unit tests.

15. **Run the Spanish case set three times** (AC3) against dev, via the Actions tab: **evaluate-analyze**, `dataset: analyze/cases-frequent-es-v1`. Keep the three `analyze-answers` artifacts.

16. **Compare `judge_failed` first** (spec task 7). If the Spanish runs fail the judge materially more often than English, stop and open a product bug: `_COMPONENTS_PATTERN` in `src/qfa/services/judge_scoring.py` matches the literal English words `faithfulness:`, `coverage:` and `clarity:`, and a Spanish reply that translates them parses as unavailable (`quality_score: null`, `JUDGE_UNAVAILABLE_EXPLANATION`). That is a service defect, not a quality gap, and it must not be read as one. Only once `judge_failed` is comparable do the `item_recall` / `citation_precision` / `group_recall` numbers mean anything. Report the three runs' `language_match` alongside.

---

## Verification

- `make test` and `make lint` pass. Every Part A step is covered by tests that need no Langfuse credentials and no LLM call, matching the existing `tests/eval/` convention (hand-made records, `SimpleNamespace` dataset items, no network).
- `uv run python eval/upload_pool.py --input .corpus_work/analyze-pool/records-en-v1.yaml --dry-run` reports **0 created, 0 updated** — proof that the English pool is untouched.
- `uv run python eval/make_analyze_cases.py --dry-run` likewise reports no change to `analyze/cases-frequent-en-v1`.
- `make docs` builds.

## Out of scope — do not do these

Codes in the record metadata, any third language, and a real-feedback test set (all named in the spec). Also, from what I found while reading: a `twin_id` consistency check on `analyze/prompts-v1` (the field is required but never validated — a separate ticket), and any re-addition of a keyword vocabulary.

## PR

Branch `feat/356-loop-add-spanish-to-the-analyze-eval-pool` is already cut. Conventional commits, subject and optional body only, no trailers. Small commits along the step boundaries above. The PR body closes #356, and must state explicitly which Part B steps were done and which were not — in particular whether the three runs and the `judge_failed` comparison happened, since AC3 is not met without them.
