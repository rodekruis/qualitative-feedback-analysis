# Implementation plan — #370: fix `analyze-bulk` doc rot (`anonymize` / `used_anonymization`)

## Direction: fix the docs, do not add the fields

The spec leaves the direction open ("Update the docs to match the schema, or add the fields to the schema if they were meant to exist"). Go with **update the docs**. `git log -S used_anonymization` settles it:

- `f0d937e` (2026-06-01) — *"refactor(api): make anonymization always-on across inference endpoints — remove request/response anonymization toggles from the public API contract"*. The fields were deliberately removed from the contract; the two table rows in `docs/rest-api/index.md` were simply missed.
- Re-adding them would be a security-relevant change (`docs/security-brief.html:281` publishes "Every AI call is wrapped anonymise → send → de-anonymise"), and `used_anonymization` would be a constant `true`. Neither belongs in a small ticket.

Anonymisation is still parameterised *below* the API boundary — `AnalyzeService.analyze_bulk/analyze_hierarchical(..., anonymize: bool = True)` and `LLMCallExecutor.anonymize_records_and_prompt(..., anonymize)` keep the flag — but `qfa.api.routes.analyze_bulk` never passes it, so the default `True` always wins. **Do not touch the service signatures**; only the HTTP-facing surface is wrong.

## Scope call I made (flag for the reviewer)

`scripts/stress_analyze.py` is the *same* leftover from `f0d937e`: it was created 2026-05-28, three days before the toggle was removed. It still puts `"anonymize": anonymize` in the `/v1/analyze-bulk` body (`build_request`, line ~161) and exposes a `--no-anonymize` CLI flag (line ~499, help text: *"Disable PII anonymisation"*). `ApiAnalyzeRequest` does not set `extra="forbid"` (only `ApiFeedbackRecordMetadata` does), so the key is **silently ignored** by the server. A flag that advertises "PII redaction off" and does nothing is worse than no flag, so step 3 removes it. It is a separate commit — drop that commit if a reviewer thinks it belongs in its own ticket; steps 1–2 and 4 stand alone.

---

## Steps (in order)

### Step 1 — Remove the two phantom rows from `docs/rest-api/index.md`

1. Delete line 36: the `| `anonymize` | bool | `true` | …` row from the **POST /v1/analyze-bulk → Request** table.
2. Delete line 56: the `| `used_anonymization` | bool | …` row from the **POST /v1/analyze-bulk → Response (200 OK)** table.
3. Add one sentence of *replacement* fact, so nobody re-adds the rows. Put it in the cross-endpoint prose block that already sits after the analyze-bulk tables (near "Empty `content` is accepted on every endpoint…", ~line 70), because it is true of every inference endpoint, not just analyze-bulk:

   > Anonymisation is unconditional on every inference endpoint: record text and the analyst prompt are redacted before the LLM call and restored in the response. There is no request field to switch it off and no response field reporting it — see [crosscutting concerns](../architecture/04-crosscutting.md).

   One sentence, no more — per `AGENTS.md`, the page must not get longer than the behaviour warrants, and the net change here is still a deletion.

**Do not** add a `## Breaking changes` entry (line 243). Nothing about the wire contract changes; the fields never existed in a released schema.

**Proof:** `grep -n 'anonymize' docs/rest-api/index.md` returns only the new prose sentence. Plus step 2's test.

### Step 2 — Add a doc-drift regression test

New file `tests/api/test_rest_api_docs.py`. This is the test that *proves* step 1 and stops the class of bug recurring. It lives in `tests/api/` to reuse the `test_app` / `client` fixtures from `tests/api/conftest.py`.

What it does — for each `## POST <path> — field reference` section in `docs/rest-api/index.md`:

- Locate the doc file with the repo-root idiom already used in `tests/scripts/test_stress_analyze.py:34`: `Path(__file__).resolve().parents[2] / "docs" / "rest-api" / "index.md"`.
- Parse the field names out of the `### Request` and `### Response (200 OK)` tables with `re.findall(r"^\| `([^`]+)` \|", body, re.M)`.
- **Request side:** assert the documented set equals `set(Model.model_fields)` for the mapped Pydantic model.
- **Response side:** POST a happy-path body through the `client` fixture, assert 200, and assert the documented set equals `set(resp.json())`.

Use the live response body, **not** `model_fields` or `model_json_schema`, for the response side: it is the only source that is unambiguously right about `@computed_field` (`pretty_output`, `quality_text`) and about `ApiSummarizeBulkResponse.output_language`, which is `Field(exclude=True)` and correctly absent from both the wire and the docs.

Parametrise over the three documented endpoints with a small table in the test module:

| doc section | request model | sample body |
|---|---|---|
| `/v1/analyze-bulk` | `ApiAnalyzeRequest` | `{"feedback_records": [{"id": "r1", "content": "Water access was limited."}], "prompt": "What are the main themes?"}` (copy `_valid_body` from `tests/api/test_analyze_endpoint.py:20`) |
| `/v1/summarize-bulk` | `ApiSummarizeBulkRequest` | `{"feedback_records": [{"id": "r1", "content": "…"}]}` (cf. `tests/api/test_routes.py:649`) |
| `/v1/summarize` | `ApiSummarizeRequest` | `_valid_summary_body()` shape from `tests/api/test_routes.py:61` |

Auth header: `{"Authorization": f"Bearer {FAKE_API_KEY}"}` from `tests/api/conftest.py`.

I verified by hand that **set equality holds in both directions for all three sections once step 1 lands** — 6/6 request fields and 13/13 response fields for analyze-bulk, 3/3 and 8/8 for summarize-bulk, 1/1 and 8/8 for summarize. So assert equality, not subset: it catches undocumented-new-field drift as well as documented-phantom-field drift.

`assign-codes`, `detect-sensitive` and the usage endpoints have no field-reference table; the section regex naturally skips them. Do not add tables for them in this ticket.

**Ordering constraint:** write the test *before* or *with* step 1, and confirm it fails on the two analyze-bulk rows first — otherwise the test proves nothing.

**Proof:** `uv run pytest tests/api/test_rest_api_docs.py -v` — 6 passing parametrised cases (3 request, 3 response).

### Step 3 — Drop the dead `anonymize` knob from `scripts/stress_analyze.py`

Four edits, all in that one file:

1. `build_request(...)` — remove the `anonymize: bool = True` parameter and the `"anonymize": anonymize` key from `body` (~lines 141, 161).
2. `run_batch(...)` — remove the `anonymize: bool = True` parameter, the `anonymize=anonymize` pass-through to `build_request` (~lines 252, 296), and the `anonymize` mention in its `Parameters` docstring line (~line 276).
3. Argument parser — delete the `--no-anonymize` argument (~line 499) and the `anonymize=not args.no_anonymize` call-site argument (~line 543).
4. Update `build_request`'s docstring only if it names the field; it currently does not.

`notebooks/analyze_corpus.ipynb` imports only `load_sample` from this module, so no notebook change is needed. No doc page references `--no-anonymize` (`docs/specs/guard-auto-deploy-on-publish.md:85` only names the test file).

**Depends on:** nothing, but keep it as its own commit after step 1 so it can be dropped independently.

### Step 4 — Update the stress-script test

`tests/scripts/test_stress_analyze.py::TestBuildRequest::test_default_body_contains_required_fields` (line ~96) currently asserts:

```python
assert set(body) == {"feedback_records", "prompt", "mode", "anonymize"}
assert body["anonymize"] is True
```

Change the set to `{"feedback_records", "prompt", "mode"}`, drop the `anonymize` assertion, and update the docstring (`"""Records, prompt, mode, anonymize are always present."""` → drop `anonymize`). Add one assertion in the same test that `"anonymize" not in body`, with a one-line why: the server removed the toggle in `f0d937e` and ignores the key.

**Ordering constraint:** must land in the same commit as step 3, or the suite is red between commits.

**Proof:** `uv run pytest tests/scripts/test_stress_analyze.py -v`.

---

## Verification

```
uv run pytest tests/api/test_rest_api_docs.py tests/api/test_analyze_endpoint.py tests/api/test_routes.py tests/scripts/test_stress_analyze.py
make test
make lint
make docs
```

`make docs` matters here: it runs `sphinx-build -W --keep-going`, so the new relative link to `../architecture/04-crosscutting.md` added in step 1 must resolve or the build fails on a warning.

**Environment caveat:** `uv sync` / `uv run` in this worktree fails building `hdbscan==0.8.43` from source — no `cc` on the box. Sort the toolchain (or reuse a prebuilt environment) before trusting `make test`; do not report green without actually running it.

## Out of scope — do not touch

- `AnalyzeService.analyze_bulk` / `analyze_hierarchical` and `LLMCallExecutor.anonymize_records_and_prompt` keep their `anonymize` parameters and their tests (`tests/services/test_analyze_hierarchical.py`, `tests/services/test_llm_call_executor.py`). Those are internal seams, correctly parameterised, and `anonymize=False` is genuinely exercised at `tests/services/test_llm_call_executor.py:252`.
- `docs/security-brief.html` — already consistent with always-on anonymisation; nothing security-related changes here.
- Field-reference tables for `assign-codes` / `detect-sensitive` — absent today, and adding them is a separate ticket.
- No version bump: releases are cut by separate `semantic_release` commits, not per PR.

## PR

Branch is already `feat/370-loop-small-ticket-fix-docs-analyze-bulk-docum`. Conventional commits, subject line plus optional body, **no trailers**. Suggested split:

1. `docs(rest-api): drop anonymize request and used_anonymization response rows` (steps 1–2)
2. `refactor(scripts): drop the no-op anonymize toggle from the stress runner` (steps 3–4)

PR body closes the issue (`closes #370`). `main` is protected — open the PR, request a human review, do not self-approve or merge.
