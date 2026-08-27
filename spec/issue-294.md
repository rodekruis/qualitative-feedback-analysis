## Context

Reported by Sabrina. An English feedback record came back with a French AI-generated
title/summary, with no French anywhere else in the record:

<img width="958" height="770" alt="Image" src="https://github.com/user-attachments/assets/9858f735-4acf-4cb6-a2d9-c3c60e02f59f" />

<img width="949" height="684" alt="Image" src="https://github.com/user-attachments/assets/5555e802-2e72-4a4d-ab8d-10b29fb9454e" />

This is the single-record `summarize()` path (`POST /v1/summarize`), driving the "Title (AI)" /
"Summary (AI)" fields shown above.

## Spec

**What:** Auto-detect each feedback record's source language server-side and pin the LLM's
summarize output to that language explicitly, for the single-record `summarize()` path
(`POST /v1/summarize`) only. No new request field — detection is entirely internal, per explicit
product decision: callers must not have to set anything for this to work.

`summarize_bulk()` (`POST /v1/summarize-bulk`) is explicitly **out of scope**: its callers always
set `output_language` explicitly in practice, so `build_output_language_instruction` already
pins its language correctly today — there is no bug to fix there, and no auto-detection or
majority-vote logic is needed for that path.

**Why:** `_DEFAULT_SUMMARIZATION_PROMPT` (`src/qfa/services/summarize.py:46-54`) only asks the
model to "use the same language as the input... unless a target language is specified" — a soft
instruction the model is free to ignore, and evidently does. Worse, `summarize()` never calls
`build_output_language_instruction` at all (`src/qfa/services/prompts.py:119-151`) —
`SingleSummaryRequestModel` (`src/qfa/domain/models.py:228-236`) has no `output_language` field,
so there is no escape hatch at all today, not even a caller-supplied one.

`langdetect` is already a project dependency (`pyproject.toml:45`) and already used this way in
`src/qfa/adapters/presidio_anonymizer.py:46-60`'s `detect_language()` — but that helper lives in
`qfa.adapters` (services must not import adapters directly) and deliberately coarsens its output
to Presidio's 6 supported spaCy models (falls back to `"xx"`), which throws away exactly the
information needed here.

### Design

- Add a small pure helper in `qfa.services` (e.g. `qfa/services/language.py`) wrapping
  `langdetect.detect()` directly — *not* the adapter's `detect_language` — returning the raw
  ISO 639-1 code (e.g. `"fr"`) or `None` on `LangDetectException` / empty input.
  `build_output_language_instruction` already embeds `output_language` verbatim into "Write the
  {subject} in {output_language}...", and an ISO code reads fine there, so no name-mapping table
  is needed.
- `summarize()` (`src/qfa/services/summarize.py:250-320`): detect the language of
  `request.feedback_record.content` before anonymization. On success, append
  `build_output_language_instruction(detected, subject="title and summary")` to
  `_DEFAULT_SUMMARIZATION_PROMPT`. On failure (`None`), leave the prompt exactly as today — no
  regression when detection can't run.
- Out of scope, no code change: `summarize_bulk()` (already correctly pinned via its always-set
  `output_language`), `analyze_bulk`, `analyze_hierarchical`, the assign-codes judge explanation
  — those already have their own `output_language` story (or deliberately don't, per #256); this
  ticket does not touch them.

### Notes for the implementer

- Do **not** import `qfa.adapters.presidio_anonymizer.detect_language` from `qfa.services` —
  breaks the adapters→services layer direction the import-linter contracts enforce, and its
  6-language/`"xx"` fallback is the wrong tool here anyway.
- Do not touch `summarize_bulk()` or its existing
  `TestAggregateSummaryOutputLanguage` tests — that path is out of scope, per the "always
  explicit `output_language`" assumption above.
- Follow the existing `FakeLLMPort` / `fake_llm.calls[0]["system_message"]` test pattern already
  in `tests/services/test_summarize.py` for the new tests, rather than introducing a new fixture
  style.

## Acceptance criteria

- [ ] `POST /v1/summarize` on an English feedback record with no other language cues reliably
      returns an English title/summary with no caller-supplied language parameter (regression
      test for the reported bug)
- [ ] `SingleSummaryRequestModel` gains no new field
- [ ] `summarize_bulk()` receives no code change and its existing
      `TestAggregateSummaryOutputLanguage` tests still pass unmodified
- [ ] `summarize()` falls back to today's soft instruction (no forced directive) when detection
      fails, rather than erroring
- [ ] New services-layer helper does not import from `qfa.adapters` (`make lint` / import-linter
      passes)
- [ ] New unit tests cover: successful detection, and detection failure/short text, for
      `summarize()`
- [ ] `docs/architecture/06-prompt-envelope.md` updated to describe the auto-detected directive
      for the single-record summarize path
