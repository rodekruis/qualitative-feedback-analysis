# Generate text bodies for the trend-detection corpus

This file has **two audiences**, in two sections:

1. **Driver instructions** — read these if you are a Claude Code agent
   asked to "run step 2" of the corpus pipeline. They describe the full
   batching loop end-to-end. After running, hand control back to the
   operator with a brief report.
2. **Per-batch prompt** — the block under `## Per-batch prompt` is what
   gets sent to the LLM in each batch call. The driver copies it
   verbatim into each batch message.

You do not need any other file (no README, no plan doc) to drive step 2 —
everything is below.

---

## Driver instructions (for the orchestrating Claude Code agent)

### Where this fits in the larger pipeline

The trend-detection corpus is built in three steps:

```text
gen-specs (Python)  →  specs.jsonl
                    ↓
LLM batches (you)   →  texts.jsonl     ← step 2, what you are doing
                    ↓
merge (Python)      →  analyze_corpus.yaml
```

Step 1 (`uv run python scripts/generate_corpus.py gen-specs`) was already
run by the operator. It produced a JSONL file of record specs, each with
full metadata + a `creation_date` (which encodes the planted trend) +
a `_context` block with the leaf code's name, description, and example
utterances. Your job is to fill in the prose `text` for every spec.

Step 3 (`uv run python scripts/generate_corpus.py merge`) is the
operator's responsibility, not yours — but the file you produce
(`texts.jsonl`) is its input, so your output schema must match exactly.

### Inputs you need

- `fixtures/analyze_corpus.specs.jsonl` — the specs to fill in. One JSON
  object per line. Default path; if the operator names a different
  file, use that.
- `fixtures/coding_framework.json` — reference for the leaf-code
  taxonomy. The specs already inline the relevant fields under
  `_context`, so you only need this if a spec's `_context` is missing
  or you want to double-check a code's meaning.

### Output you produce

- `texts.jsonl` (in the repo root unless the operator specifies
  otherwise) — one JSON object per line, exactly:
  ```json
  {"id": "doc-0001", "text": "...", "sentence_count": 3}
  ```
- Same `id` set as the specs file; same line count; order does not
  matter (the merge step joins by `id`).

### The batching loop

1. **Count specs.** `wc -l fixtures/analyze_corpus.specs.jsonl`. Confirm
   it matches what the operator expected (default: 5000).
2. **Decide batch size.** Default to 100 records per batch. Smaller is
   safer per call but multiplies the number of calls. Don't go above
   200 — long context risks the LLM losing track of element ordering
   and dropping records.
3. **Open `texts.jsonl` for append.** If it already exists from a
   previous run, ask the operator whether to resume (skip ids already
   present) or restart (truncate first). Default: resume — the prompt
   is deterministic enough that re-running missing ids is cheap.
4. **For each batch:**
   - Read the next N spec lines into a JSON array literal.
   - Construct a single user-turn message that contains *the entire
     per-batch prompt block below*, immediately followed by a line
     reading `specs:`, then the JSON array on its own line.
   - Send the message. Wait for the response.
   - Parse the response as a JSON array. If parsing fails (most often:
     the model wrapped the array in a markdown fence, or added a
     preamble), strip the fence and retry parsing once; if still
     broken, regenerate the batch with the same prompt rather than
     hand-editing the output.
   - Validate: the returned array's length equals the batch size, and
     every returned `id` appears in the batch's input ids. If not,
     regenerate the batch.
   - Append each element as one line to `texts.jsonl`.
5. **At the end**, run:
   ```bash
   wc -l fixtures/analyze_corpus.specs.jsonl texts.jsonl
   ```
   The two counts must match. Also spot-check 3–5 random records
   visually — read the spec's `_context.code_name` and the text you
   produced, and confirm they would plausibly co-occur.

### Resilience and cost

- The driver does not need to retry whole runs — `texts.jsonl` is
  append-only and each batch is independent, so a crash mid-run is
  recoverable by re-reading existing ids and skipping them.
- Do not parallelise across multiple Claude Code sessions sharing the
  same `texts.jsonl` — concurrent appends race. One driver at a time.
- The operator is on a Claude Code subscription, so cost is implicit;
  prefer fewer, larger batches over many tiny ones to amortise the
  per-call overhead of re-sending the prompt body.

### Reporting back

When done, give the operator one short message:

- batches run / total
- specs in / texts out (should match)
- 1-line note on anything you regenerated or skipped
- the exact `uv run python scripts/generate_corpus.py merge ...` line
  they should run next to assemble the final YAML

---

## Per-batch prompt

Send the block below to the LLM in each batch call. Append the batch's
spec array as raw JSON after the line reading `specs:`.

> You are writing realistic English-language community-feedback records for a
> humanitarian-response benchmark. The dataset is COVID-19-era feedback
> collected by Red Cross / Red Crescent operators in West and Central Africa.
> You are not anonymising or paraphrasing real data — every record is
> synthetic.
>
> ### Inputs
>
> You receive a JSON array `specs`, where each element is one record's full
> metadata:
>
> ```json
> {
>   "id": "doc-0001",
>   "metadata": {
>     "dataset": "COVID-19",
>     "feedback_type": "Observation, perception or belief",
>     "region": "Western Area",
>     "country": "Sierra Leone",
>     "language": "en",
>     "source": "hotline",
>     "year": 2020,
>     "sensitive": false,
>     "codes": "covid-19:observation-perception-or-belief:...:..."
>   },
>   "_context": {
>     "code_name": "Belief that COVID-19 is caused by 5G",
>     "code_description": "...",
>     "code_examples": ["...", "..."],
>     "trend_role": "spike|emerging|declining|step|cross_code|rumour|baseline"
>   }
> }
> ```
>
> The `_context` block is **for your reading only** — do not echo it back. It
> tells you which leaf the record codes for, what that code means, a few
> real-world example utterances, and which planted trend (if any) the record
> belongs to.
>
> ### Output
>
> Return a JSON array `texts` of the same length and in the same order as
> `specs`. Each element is:
>
> ```json
> {
>   "id": "doc-0001",
>   "text": "Caller, a 58-year-old woman from Freetown, reports that…",
>   "sentence_count": 3
> }
> ```
>
> Emit **only** the JSON array. No prose, no preamble, no markdown fences.
>
> ### Style guide
>
> The reference register is operator-paraphrase casework notes — the same
> register a hotline operator types into a CRM after a call. It is not survey
> prose, not a focus-group quote, and not first-person diary writing (except
> where the `source` field says otherwise; see below).
>
> - **Length.** 100–500 characters, median ~200. Two to four sentences for
>   most records. A small minority (< 5 %) may run longer for complex cases.
> - **Structure.** Most records have two short paragraphs separated by a
>   newline:
>   1. *Caller situation & request* — demographics inline, request paraphrased.
>      `"Woman, 62, IDP from Bo District, reports that her neighbours believe
>       the vaccine causes infertility."`
>   2. *Operator action / referral* — what was offered, explained, or who the
>      caller was referred to. `"Operator clarified WHO guidance and provided
>      hotline number for district health office."`
> - **Voice by `source`**:
>   - `hotline`, `outbound call`, `email`, `web form` → third-person operator
>     paraphrase. Compact, clinical. ~80 % of the corpus.
>   - `survey` → short third-person summary of a respondent's answer.
>   - `focus group`, `community feedback session`, `volunteer report` →
>     third-person summary, but may quote one short utterance ("…said: 'we
>     don't trust the new vaccine'…"). Quoted utterances should appear in
>     ~10 % of records overall.
> - **Acronyms.** Casework notes are dense with acronyms — *use them*: WHO,
>   IFRC, MSF, CDC, MoH, ETC, CHW, IDP, RC (Red Cross), PPE, CHS, RCCE.
>   Expand on first use only when natural.
> - **Realistic PII.** This corpus is the **input** to a downstream
>   anonymisation step; it must give the anonymiser something to detect.
>   Include realistic names (regionally plausible — Mariama Conteh, Jean-Paul
>   Mukendi, not "John Smith"), village/neighbourhood names, phone numbers
>   (`+232 76 543 210`, `+243 81 234 5678`), facility names ("Connaught
>   Hospital", "Goma Health Centre"). **Do not** use placeholders like
>   `[NAME]` or `[ADDRESS]`; write the names out. The downstream Presidio
>   port is what redacts them.
> - **Language.** Always English. `metadata.language` will be `"en"` for every
>   record in this corpus; if you ever see anything else, write the prose in
>   English anyway and ignore the field.
> - **Code fidelity.** The `text` must be a *plausible feedback entry that
>   would be coded under the given leaf*. Re-read `_context.code_name` and
>   `_context.code_description` for each record. Treat `_context.code_examples`
>   as canonical surface realisations: your prose should sit in the same
>   semantic neighbourhood without parroting the examples verbatim.
> - **Sensitive content.** If `metadata.sensitive` is `true`, the prose must
>   warrant the flag — mention of death, identifiable household illness,
>   GBV, child safeguarding, etc. Keep it factual, not graphic.
> - **Comma-separated `codes`.** If `metadata.codes` lists multiple
>   colon-paths (comma-separated), the prose must legitimately cover *all* of
>   them. Use a slightly longer record (~300–500 chars) in that case.
> - **Trend-role neutrality.** Do **not** mention the `_context.trend_role`
>   in the prose. Trends are encoded only by which dates the spec
>   assigned — your job is to write text that, taken on its own, looks
>   organically generated. A `rumour`-role record should not say "rumour".
> - **No template clones.** Vary opening verbs across the batch: *Caller
>   asks…*, *Beneficiary requests…*, *Woman, IDP, reports that…*,
>   *Family member enquires whether…*, *Mother of three calls about…*,
>   *Operator follows up on previous outbound about…*
>
> ### Self-check before emitting
>
> For each output element:
>
> 1. `text` runs 100–500 characters and ~2–4 sentences (or longer if `codes`
>    is multi-valued).
> 2. `sentence_count` equals the actual number of sentences in `text`.
> 3. The prose plausibly fits under the leaf code in `_context`.
> 4. Voice matches `metadata.source` per the rules above.
> 5. PII shape matches the rule (realistic names, no placeholders).
> 6. No mention of `trend_role` or any internal pipeline metadata.
>
> Begin emitting the JSON array now.
>
> specs:
