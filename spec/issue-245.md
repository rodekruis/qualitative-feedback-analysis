Currently we delete unsupported characters in espo, e.g.

// Turn every whitespace variant into a plain space
$feedbackDescription = string\replace($feedbackDescription, "\n", " ");
$feedbackDescription = string\replace($feedbackDescription, "\r", " ");
$feedbackDescription = string\replace($feedbackDescription, "\t", " ");
$feedbackDescription = string\replace($feedbackDescription, "\f", " ");
$feedbackDescription = string\replace($feedbackDescription, "\v", " ");

if we would like to move to a different system then it would be better to do this at the api side. Question, is that possible to send request to the endpoints that contain these strings?


---

## Investigation: root cause + fix options (2026-08-13)

*Analysis only — nothing implemented yet. Verified against `origin/main` @ `57bb89f`.*

### TL;DR

**Yes, you can send feedback containing `\n`, `\r`, `\t`, `\f`, `\v`.** The API accepts all of them
today — as long as they are **JSON-escaped in the request body**.

The 422 is *not* the API rejecting those characters. It is the API failing to **parse the request
body**, because the EspoCRM formula builds the JSON payload with `string\concatenate` and
interpolates raw field values straight into it. Any character that must be escaped inside a JSON
string breaks the whole document, and the request never reaches application code.

That last point answers the question in the title: **a regex/sanitiser "at the API side" cannot fix
this.** The body dies in the JSON parser *before* any route handler, Pydantic model, or validator
runs. There is no place to put a regex that would see this input. The only server-side lever is
changing *how the body is parsed* (Option 2 below), not what it validates.

### Confirmed

Reproduced against `POST /v1/analyze-bulk` with a real app instance (fake orchestrator, real
routes/middleware/exception handlers):

| # | Request body | Result |
|---|---|---|
| A | `content` contains `\n \r \t \f \v`, properly JSON-escaped (`json.dumps`) | **200 OK** |
| E | `content` is `"a\nb"` with a valid two-character `\n` escape | **200 OK** |
| B | raw literal newline inside the JSON string | **422** `validation_error`, field `body.58`, issue `JSON decode error` |
| C | raw literal tab inside the JSON string | **422** (same shape) |
| D | lone backslash / invalid escape (`"path C:\dir"`) | **422** (same shape) |
| F | unescaped `"` inside `content` (`"he said "hi""`) | **422** (same shape) |

Case B/C/D/F is exactly the 422 reported above. Case D is also why *"I just got rid of all
backslashes in the espoCRM flow and it works"* helped — a lone `\` in the feedback text becomes an
invalid escape sequence once it is pasted into hand-built JSON.

### Root cause

The payload is assembled as **text**, not serialised as JSON:

`scripts/espo_crm/insight_trigger/1_set_feedback_records_string.php` (L23-40):

```php
// Clean the feedback description string
$feedbackDescription = string\replace($feedbackDescription, "\n", " ");
$feedbackDescription = string\replace($feedbackDescription, "\r", "");
...
$record = string\concatenate(
    '{"content": "', $feedbackDescription, '", "id": "', $feedbackID, '", ',
    '"metadata": ', $metadata, '}'
);
```

Same pattern in `feedback_trigger/1_set_feedback_record_string.php` and in both
`3_combine_mother_payload.php` files.

Per RFC 8259, a JSON string must escape `"`, `\`, and **every** control character U+0000–U+001F
(that includes `\n` U+000A, `\r` U+000D, `\t` U+0009, `\b` U+0008, `\f` U+000C, and `\v` U+000B).
Interpolating an unescaped value into a quoted string produces an invalid document.

Two things make this broader than the issue title suggests:

1. **The existing cleanup is incomplete.** The scripts on `main` only replace `\n` and `\r`.
   `\t`, `\f`, `\v`, `"` and `\` still break the payload.
2. **It is not only `feedbackDescription`.** `$feedbackID`, `$createdAt`, `$codingLevel1..3`,
   `$prompt` and `$outputLanguage` are all interpolated unescaped too. A single `"` or `\` in a code
   label or in the free-text prompt breaks the call in exactly the same way.

### The fix already exists in this repo

`scripts/espo_crm/feedback_trigger/2_set_codes_string.php` builds its payload the correct way —
it constructs objects and lets Espo serialise them:

```php
$node3 = object\create();
$node3['id'] = $id3;
$node3['name'] = $name3;
...
$$codesString = json\encode($rootCodes);
```

So the Espo formula language **has** `object\create()` + `json\encode()`, and we already rely on it
for the coding framework. The feedback-record builders are the odd ones out.

### Secondary finding: two inputs *are* rejected server-side

Even with perfectly valid JSON, two classes of content are rejected by the injection detector in
`LiteLLMClient._check_injection` (`src/qfa/adapters/llm_client.py` L141-157), which
`_handle_analysis_error` (`src/qfa/api/app.py` ~L409) maps to **422 `validation_error`**:

- **NUL** (U+0000) → `pattern=null_byte`
- **200+ consecutive identical characters** → `pattern=repeated_chars` (e.g. a long `------` divider
  row pasted into a feedback description)

Verified that `\n \r \t \f \v` do **not** trip these. So the whitespace variants named in this issue
are genuinely safe; NUL and long character runs are not, and they fail with a *different* 422 body
(`message: "Prompt injection detected pattern=..."`) that is easy to tell apart from a parse failure.

### Fix options

**Option 1 — serialise properly in Espo (recommended).**
Rewrite `feedback_trigger/1_set_feedback_record_string.php`,
`insight_trigger/1_set_feedback_records_string.php` and both `3_combine_mother_payload.php` to use
`object\create()` / `list()` + `json\encode()`, following the pattern already proven in
`2_set_codes_string.php`. Delete the `string\replace` cleanup — escaping becomes the serialiser's
job and the feedback text survives intact (line breaks included, which the API accepts).
*Pros:* fixes every case (control chars, quotes, backslashes), no data loss, keeps the API a
standard JSON API, no server change, uses a pattern already in the repo.
*Cons:* touches four scripts; a future move to another system must also serialise properly — but
that is true of any HTTP client and is cheaper than the current hand-rolled cleanup.

**Option 2 — tolerate raw control characters server-side (narrow, additive).**
Parse the body with `json.loads(..., strict=False)` via a custom `Request`/`APIRoute` class.
Verified behaviour: `strict=False` **accepts** raw control characters (so it fixes exactly the
`\n \r \t \f \v` class named in this issue) but **still rejects** invalid escapes like `\d` and
unescaped `"`.
*Pros:* every current and future client gets slack for the most common breakage, with no client
change.
*Cons:* it is only a partial fix — Espo would still need escaping for `"` and `\`; it accepts
technically-invalid JSON, creating a private dialect that diverges from our OpenAPI contract and
from any spec-compliant client or generated SDK. Needs an ADR because it changes the HTTP boundary
contract.

**Option 3 — make the 422 self-explanatory (cheap, worth doing regardless).**
Today a malformed body yields field `body.58` and issue `JSON decode error` — a byte offset and no
hint about the cause. Surface the underlying `JSONDecodeError` reason instead, e.g. code
`json_invalid` with *"Invalid JSON at byte 58: unescaped control character U+000A inside a string —
JSON strings must escape `\n`, `\r`, `\t`, `"` and `\`."* This would have turned the present
investigation into a five-minute fix on the Espo side.

**Option 4 — sanitise with a regex inside the API's Pydantic models (rejected).**
Cannot work: the body fails to parse before any validator runs. Recorded here so it is not
re-proposed.

### Recommendation

**Option 1 + Option 3.** Option 1 removes the class of bug at its source; Option 3 makes the next
occurrence self-diagnosing. Take Option 2 only as a deliberate product decision that we want to
accept sloppy JSON from low-capability clients — and if so, it is *in addition to* Option 1, not
instead of it.

### Scope if we proceed

- `scripts/espo_crm/feedback_trigger/1_set_feedback_record_string.php`
- `scripts/espo_crm/feedback_trigger/3_combine_mother_payload.php`
- `scripts/espo_crm/insight_trigger/1_set_feedback_records_string.php`
- `scripts/espo_crm/insight_trigger/3_combine_mother_payload.php`
- Option 3: `_handle_validation_error` in `src/qfa/api/app.py`
- Regression tests (`tests/api/test_routes.py`): a raw-control-character body returns 422 with the
  new code, **and** a body with properly escaped `\n \r \t \f \v` returns 200 — the second test pins
  the "we do accept line breaks in feedback" contract so nobody adds a character filter later.
- Docs: `docs/integrations/espo-crm.md` (currently says nothing about escaping — add the
  "always `json\encode`, never `string\concatenate`" rule) and `docs/rest-api/` (state that
  `content` may contain any character provided it is JSON-escaped, and document the NUL /
  200-repeat rejections).

### Open question for @olafdegraaff / @mariushelf

Is there any client in the chain that genuinely *cannot* produce escaped JSON (i.e. is Option 2
actually needed), or is Espo the only producer and Option 1 sufficient? Espo can clearly do it —
`2_set_codes_string.php` already does.
