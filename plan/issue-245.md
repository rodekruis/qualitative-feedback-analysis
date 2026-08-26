# Implementation plan — #245 (regex at API side)

## Before you start: one spec/tree mismatch, and how I resolved it

The spec's Option 1 names four files that **no longer exist**:

```
scripts/espo_crm/feedback_trigger/1_set_feedback_record_string.php
scripts/espo_crm/feedback_trigger/3_combine_mother_payload.php
scripts/espo_crm/insight_trigger/1_set_feedback_records_string.php
scripts/espo_crm/insight_trigger/3_combine_mother_payload.php
```

Commit `7c52ed8` ("docs: keep only flowchart CSVs for EspoCRM scripts", 2026-08-18 — five days *after* the spec was written) deleted every `.php` mirror. The formula scripts themselves were not lost: they are embedded, JSON-escaped, in the `data` column of the two flowchart CSVs that remain in the tree. I verified the exact defect the spec describes is still there — `string\concatenate` appears 12× in `Feedback_saving_flowchart.csv` and 6× in `Insight_creation_flowchart.csv`, building JSON by hand, with the same incomplete `string\replace` cleanup in front of it.

**Decision: retarget Option 1 at the two CSVs.** The artifact moved, the code did not; this is not a missing foundation, so it is not a blocker. `docs/integrations/espo-crm.md` already declares the CSV "the only maintained copy of a flow's formula script".

Two more corrections to the spec you will hit while writing docs:

- The spec says injection detection maps to `422 validation_error` via `_handle_analysis_error`. Stale — ADR-018 landed since, and `src/qfa/api/app.py:438` `_handle_prompt_injection_detected` now returns `422 prompt_injection_detected`. Document the real code.
- The spec names two rejected inputs; there are three patterns in `LiteLLMClient._check_injection` (`src/qfa/adapters/llm_client.py:225-232`): `role_prefix` (content starting `SYSTEM:`/`ASSISTANT:`/`USER:`), `null_byte`, `repeated_chars`.

The spec ships no explicit acceptance-criteria checklist; the ACs below are derived from its "Scope if we proceed" section.

## Decisions the spec left open

| # | Decision | Why |
|---|---|---|
| D1 | **Option 2 is out.** Not implemented, not stubbed. | The spec makes it conditional on a product answer from @olafdegraaff / @mariushelf that this headless run cannot obtain, and states it is only ever *in addition to* Option 1. Option 1 removes the need. |
| D2 | **Do not echo `ctx["error"]` into the response.** Map the stdlib reason to hint strings authored in this repo, matched by **prefix**. | ADR-018 rule 3. Also concrete: CPython's *pure-Python* scanner produces `Invalid control character %r at` / `Invalid \escape: {0!r}` — a `repr` of caller-supplied bytes. The C accelerator's shorter forms are safe today, but the mapping must not depend on which scanner is loaded. Prefix matching covers both variants. |
| D3 | **`error.code` becomes `json_invalid`** for body-parse failures; `validation_error` stays for real Pydantic failures. | The spec's Option 3 wording. Espo reads `bpm\caughtErrorCode()` (HTTP status), not the body code, so no client breaks. |
| D4 | `fields` carries **one entry, `field: "body"`**, with the hint plus the byte offset in `issue`. | Today it is `field: "body.58"` — the offset smuggled into a field path. FastAPI only preserves `e.pos`, not `lineno`/`colno`, so promise a byte offset and nothing more. |
| D5 | The new logic lives in **`src/qfa/api/app.py`** as a module constant plus one private function — no new module. | Every handler and `RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS` already live there; a new module would need an `import-linter` review for no gain. |
| D6 | **Espo flow variables hold objects/lists, not pre-encoded JSON strings**, and each mother payload is a single `json\encode` at the end. | The alternative — splicing already-encoded JSON documents with `string\concatenate` — is structurally safe but leaves the concatenation idiom in place for the next person to extend with a raw field value. `$` and `$$` are demonstrably the same store in this flow (`combine` sets `$urlAssignCodes`, the request node reads `{$$urlAssignCodes}`), and Espo persists process variables as JSON. |
| D7 | **Add a guard test over the flowchart CSVs** (`tests/scripts/test_espo_flowcharts.py`), beyond the spec's scope. | Otherwise step 5 has no verification in CI at all. Kept to three assertions. |
| D8 | **No new ADR.** | Option 2 would have changed the HTTP boundary contract and needed one. Changing an error code and message does not; both existing error-mapping tables get a row. |
| D9 | **No `docs/security-brief.html` update.** | The brief's sections are auth, PII, secrets, audit, network, dev process. Nothing in those changes: no input is newly accepted at the LLM boundary (the injection patterns are untouched), and the new messages are repo-authored constants. Stated explicitly because AGENTS.md makes this conditional, not optional. |

`spec/issue-245.md` is already committed on this branch (`4bc5da7`) — leave it.

## Work

Branch `feat/245-loop-spike-regex-at-api-side` already exists off `origin/main`. Two commits, one PR.

---

### Step 1 — Make a malformed body say why it was rejected

**File:** `src/qfa/api/app.py`

1a. Add a module-level constant near `RATE_LIMIT_RETRY_AFTER_FALLBACK_SECONDS`:

`_JSON_DECODE_HINTS: tuple[tuple[str, str], ...]` — ordered `(stdlib-msg-prefix, repo-authored hint)` pairs. Order matters: `"Invalid control character"` must precede `"Invalid \\"`.

| Prefix to match | Hint (write the final wording yourself; these are the facts each must carry) |
|---|---|
| `Invalid control character` | a raw control character sits inside a JSON string; they must be escaped (`\n` `\r` `\t` `\f` `\u000b` `\^@`); **the API accepts these characters once escaped — do not strip them** |
| `Invalid \` | a backslash is not part of a valid escape sequence; a literal backslash is `\\` |
| `Unterminated string starting at` | a string is never closed; a literal `"` inside a string must be `\"` |
| `Expecting ',' delimiter` / `Expecting ':' delimiter` / `Expecting property name enclosed in double quotes` | unexpected token, most often an unescaped `"` ending a string value early |

Plus `_JSON_DECODE_FALLBACK_HINT` for everything else (`Expecting value`, `Extra data`, anything a future CPython adds).

1b. Add `def _json_decode_hint(reason: str) -> str` — first prefix match wins, fallback otherwise. Docstring must record *why* the reason string is mapped rather than echoed (ADR-018 + the `repr`-embedding pure-Python scanner). This is the one added fact the signature doesn't carry.

1c. In `_handle_validation_error` (`app.py:319`), before the existing loop: if `exc.errors()` is non-empty and **every** entry has `type == "json_invalid"`, build and return the parse-failure response instead —

- `code="json_invalid"`, `message="Request body is not valid JSON"`
- one `ApiErrorFieldDetail` per error: `field="body"`, `issue=_json_decode_hint(err.get("ctx", {}).get("error", "")) + " (byte offset N)"` where `N = loc[1]` when `loc` is `("body", <int>)`, omitted otherwise
- status stays 422

Never read `exc.body` — FastAPI attaches the entire raw request body there (`body=e.doc`).

Leave the existing Pydantic path byte-identical.

**Tests:** new class `TestMalformedJsonBody` in `tests/api/test_routes.py`. Post raw bytes: `client.post("/v1/analyze-bulk", content=b"...", headers={"Content-Type": "application/json", **_auth_header()})`. Auth must be valid — FastAPI resolves dependencies before the body, so a bad key yields 401 and the test proves nothing.

1. raw newline inside `content` → 422, `code == "json_invalid"`, `fields[0]["field"] == "body"`, issue mentions escaping
2. raw tab → 422 `json_invalid`
3. `"path C:\dir"` (lone backslash) → 422 `json_invalid`, issue mentions backslash
4. unescaped `"` inside `content` → 422 `json_invalid`
5. truncated body `{"feedback_records":` → 422 `json_invalid` with the fallback hint — pins that an unmapped reason degrades instead of raising
6. **leak pin:** body containing `SENTINEL` (already defined at `tests/api/test_routes.py:23`) *and* a raw control character → 422, and `SENTINEL not in resp.text`. This is the test that stops a future edit from reaching for `exc.body`.
7. **contract pin (the spec's headline requirement):** `json.dumps` a body whose `content` is `"line1\nline2\ttab\rcr\fff\vvt"` → **200**, and `fake_service.last_analyze_request.feedback_records[0].content` still contains every one of those characters. This is the test that stops anyone adding a character filter later. `FakeService.last_analyze_request` already exists (`tests/api/conftest.py:138`).

`TestValidation`'s existing seven `validation_error` assertions must still pass unmodified — that is the proof D3 didn't over-reach.

---

### Step 2 — Pin the injection patterns the docs are about to claim

**File:** `tests/adapters/test_llm_client.py`

`LiteLLMClient._check_injection` has no direct test. Step 3 documents its behaviour as an API contract, so pin it first. New class, using the existing `_make_client()` helper:

- `\x00` in the user message → `PromptInjectionDetectedError`
- 200 consecutive identical characters → `PromptInjectionDetectedError`
- content starting `SYSTEM:` → `PromptInjectionDetectedError`
- a message containing `\n \r \t \f \v` and a 199-character run → **no** raise

Ordering: must land with or before Step 3.

---

### Step 3 — API docs

**`docs/rest-api/index.md`** — in the `## Error envelope` section:

- error-code table (~L163): `422` row becomes `` `validation_error`, `json_invalid`, `prompt_injection_detected`, `content_policy_violation` ``
- new short subsection, **Request body encoding**, stating as a table or list: `content` (and every other string field) may contain **any** character including line breaks, tabs and quotes, provided the body is valid JSON per RFC 8259 — control characters `U+0000`–`U+001F`, `"` and `\` must be escaped; a body that fails to parse returns 422 `json_invalid` with the reason in `fields[0].issue`; and, separately, three content classes are rejected *after* parsing with 422 `prompt_injection_detected` — NUL, 200+ consecutive identical characters, and content beginning `SYSTEM:`/`ASSISTANT:`/`USER:`. One sentence must say plainly: **do not strip whitespace characters client-side; escape them.**

**`docs/architecture/04-crosscutting.md`** — Error → HTTP mapping table (~L93): new row above the Pydantic row, `Malformed JSON request body | 422 | json_invalid`.

Keep both additions short. Neither page may grow by more than the facts above.

**Commit 1** (Steps 1–3): `fix(api): report why a malformed JSON body was rejected`

---

### Step 4 — Guard test for the flowchart payloads

**New file:** `tests/scripts/test_espo_flowcharts.py`

Helper: parse each CSV with `csv.DictReader`, `json.loads(row["data"])`, walk `data["list"]` → `node["actionList"]` → actions where `type == "executeFormula"`, yield `action["formula"]`.

Three tests, parametrized over both CSVs:

1. `test_no_json_literals_in_formulas` — no formula contains `'{"`, `'{'` or `"{`. These are the concatenation-of-JSON-brace signatures; their absence means no payload is hand-assembled.
2. `test_feedback_text_is_not_rewritten` — `string\replace` appears in no formula. Assertion message must explain: escaping is the serialiser's job, and stripping whitespace destroys feedback text.
3. `test_request_bodies_come_from_json_encode` — for every action with `type == "sendRequest"`, take `contentVariable` (e.g. `$motherPayload`), and assert some formula in the same flowchart matches `` rf"\${{1,2}}{name}\s*=\s*json\\encode\(" ``. This is the real contract; the other two are the smells.

**Verification the test works:** with the CSV edits stashed, all three must **fail** against the current tree. Confirm that before committing — a guard test that was green on the broken input guards nothing.

---

### Step 5 — Rewrite the flowchart payload builders

**Files:** `scripts/espo_crm/flowcharts/Feedback_saving_flowchart.csv`, `scripts/espo_crm/flowcharts/Insight_creation_flowchart.csv`

Each CSV is exactly **two lines** (header + one row, LF endings); every formula is JSON-escaped inside the `data` cell, which is itself CSV-quoted. In the raw bytes that means: a newline is `\n`, a `"` is `\""`, a backslash is `\\`, and a `/` is `\/` (Espo's PHP `json_encode` escapes slashes).

**Edit method — do not rewrite the file with `csv.writer`.** A round-trip would rewrite the entire single-line row and Python's `json.dumps` does not reproduce Espo's `\/` convention, producing an unreviewable diff. Instead do **literal in-place substitution on the raw file text**: locate the escaped form of each formula fragment (grep it out first), and write the replacement in the same escaping convention. After each file, verify by re-parsing: `csv.DictReader` → `json.loads(row["data"])` must succeed, the file must still be two lines, and every node *other than* the ones you targeted must compare equal to the same node parsed from `git show HEAD:<path>`.

#### 5a. `Feedback_saving_flowchart.csv`

**Node `qmgzrskmcs` "set $$recordString"** — delete all seven `string\replace` calls; build objects. Target:

```
ifThen($feedbackDescription == null, $feedbackDescription = '');
ifThen($feedbackID == null, $feedbackID = '');
ifThen($createdAt == null, $createdAt = '');

$metadata = object\create();
$metadata['created'] = $createdAt;
$metadata['coding_level_1'] = $codingLevel1;
$metadata['coding_level_2'] = $codingLevel2;
$metadata['coding_level_3'] = $codingLevel3;

$feedbackRecord = object\create();
$feedbackRecord['content'] = $feedbackDescription;
$feedbackRecord['id'] = $feedbackID;
$feedbackRecord['metadata'] = $metadata;

$$feedbackRecord = $feedbackRecord;
```

**The `ifThen` null coercions are load-bearing, not defensive noise.** `ApiFeedbackRecordInput.id` / `.content` / `.url_id` and `ApiFeedbackRecordMetadata.created` are non-nullable `str` (`src/qfa/api/schemas.py:380-408`, `:355`). Today's concatenation renders a null Espo field as `""`; `json\encode` would render it as `null` and 422 the request. `coding_level_1..3` are `str | None` — leave those uncoerced.

**Node `g3dcslcmvk` "set $$codesString"** — leave the tree-building loop alone; change only the last line from `$$codesString = json\encode($rootCodes);` to `$$rootCodes = $rootCodes;`.

**Nodes `7k24bkfmi6`, `816sh17k5p`, `20wynscwjv` (the three `combine $motherPayload`)** — replace each `string\concatenate` payload with one object build ending in `$motherPayload = json\encode($payload);`. For `7k24bkfmi6` (assign-codes) that includes `$codingLevels = object\create(); $codingLevels['root_codes'] = $$rootCodes; $payload['coding_levels'] = $codingLevels;` plus `max_codes = 1`, `confidence_threshold = 0.1`. The other two carry `feedback_record` + `confidence_threshold = 0.1` only. Do **not** touch the `$baseUrl` / `$url*` lines in those nodes — those build URLs, not JSON.

Rename the node `text` labels that name the renamed variables (`set $$recordString` → `set $$feedbackRecord`, `set $$codesString` → `set $$rootCodes`). A variable named `…String` that holds an object is a trap for the next editor.

#### 5b. `Insight_creation_flowchart.csv`

**Node `ski0qsd6qx` "set $recordsString"** — delete all seven `string\replace` calls and the comma-join. Build `$feedbackRecords = list()` and `array\push` one object per record (`content`, `id`, `url_id`, `metadata`), keeping the existing skip-if-empty-or-null branch and adding `ifThen($feedbackID == null, ...)` / `ifThen($createdAt == null, ...)` / `ifThen($urlID == null, ...)` coercions. End with `$$feedbackRecords = $feedbackRecords;`.

**Node `rzhpnsh7eu` "combine $motherPayload"** — the highest-risk node in either flow: it splices `$prompt` (user-authored free text from `freeTextPrompt`) straight into a string literal. Rebuild as an object with `feedback_records` (`$$feedbackRecords`), `prompt`, `espo_feedback_base_url`, `output_language`, `selected_method`, `endpoint`, ending in `$motherPayload = json\encode($payload);`.

Leave node `9levq1av1e` ("select $fullEndpoint") untouched — it builds a URL.

**Do not touch** the `sendRequest` nodes. `contentVariable = $motherPayload` still receives a string; `json\encode` returns one.

#### 5c. What this does *not* prove

No test in this repo can execute EspoCRM Formula Script. Step 4 proves the CSVs parse and that every request body is `json\encode` output; it cannot prove Espo evaluates the new formulas correctly, and D6 (objects surviving in `$$` process variables) is reasoned from the flow's own `$`/`$$` interchangeability, not observed. **The PR must not be merged on CI alone.** See Step 7.

---

### Step 6 — EspoCRM integration docs

**File:** `docs/integrations/espo-crm.md`

Add a short subsection under **What the flows do** (before "Error handling") stating the rule as a rule: request bodies are built with `object\create()` / `list()` and serialised with a single `json\encode()`; never assembled with `string\concatenate`. Two facts must be in it:

- `string\concatenate` of raw field values produces invalid JSON the moment feedback text contains a line break, tab, `"` or `\` — the request then dies in the parser with 422 `json_invalid` before any route handler runs, which is why no server-side sanitiser can fix it (this is the answer to the issue's actual question, and it belongs where the next integrator will look).
- Fields the API declares non-nullable (`id`, `content`, `url_id`, `metadata.created`) must be coerced to `''` before serialising, because `json\encode` emits `null` where concatenation emitted `""`.

Link `../rest-api/index.md#request-body-encoding`. Do not restate the escaping rules there.

**Commit 2** (Steps 4–6): `fix(espo): build flowchart request bodies with json\encode`

---

### Step 7 — Verify and open the PR

Run, in order: `make lint` (ruff + `ty` + `lint-imports`), `make test`, `make docs`.

Open the PR to `main` with `closes #245`. The body must contain:

1. The **before/after formula text** for each of the seven edited nodes, extracted from the CSVs and rendered unescaped. A reviewer cannot review escaped JSON inside a CSV cell; give them the formulas.
2. An explicit, unchecked **manual verification required before merge** block:
   - import both CSVs into the dev EspoCRM (Import → Flowcharts → Create & Update, per the existing doc procedure)
   - save a feedback record whose description contains a line break, a tab, a `"` and a `\` → the three per-record calls return 200 and the stored text still contains all four characters
   - save a feedback record with an empty description and a null `feedbackFormID` → still 200 (this is the null-coercion check)
   - create an insight over ≥2 records with a free-text prompt containing a `"` → 200 and `pretty_output` populated
   - EspoCRM must be **9.2.3+**, per the version requirement already in the integration doc

---

## Acceptance criteria (derived — the spec ships no checklist)

- [ ] A body with raw `\n` / `\r` / `\t` / `\f` / `\v` returns 422 `json_invalid` whose `fields[0].issue` names the cause and says the characters must be escaped, not stripped
- [ ] A body with a correctly escaped `\n \r \t \f \v` returns **200** and the service receives every character intact
- [ ] A lone backslash and an unescaped `"` each return 422 `json_invalid` with a hint specific to that cause
- [ ] An unrecognised parse failure returns 422 `json_invalid` with the fallback hint — no crash, no 500
- [ ] No caller-supplied byte appears in any 422 body (sentinel test)
- [ ] Semantic Pydantic failures still return `validation_error`; the seven existing `TestValidation` assertions pass unmodified
- [ ] Neither flowchart CSV contains `string\replace` or a hand-assembled JSON literal; every `sendRequest` body is `json\encode` output; all three guard tests fail against the pre-edit CSVs
- [ ] `docs/rest-api/index.md`, `docs/architecture/04-crosscutting.md` and `docs/integrations/espo-crm.md` updated in the same PR; `make docs` clean
- [ ] `make lint` and `make test` clean
- [ ] PR body carries the unchecked manual-verification block

## Ordering constraints

- 1 → 2 → 3 (docs describe the shipped code and the pinned injection behaviour)
- 4 before 5 **for verification** (confirm red), same commit
- 5 → 6 (docs describe the shipped formulas)
- 7 last
- Steps 1–3 and Steps 4–6 are otherwise independent; either commit can be written first

## Out of scope

Option 2 (`json.loads(strict=False)` via a custom `APIRoute`) — see D1. Option 4 was already rejected in the spec and is unimplementable by construction: the body never reaches a Pydantic validator.
