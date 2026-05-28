# Prompt: generate text bodies for a deterministic corpus spec

This prompt is the **LLM half** of the trend-detection corpus pipeline. The
full end-to-end workflow (where this file fits, how to run the surrounding
Python, how to batch the LLM calls in Claude Code) is documented in
[`scripts/README.md`](README.md) — read that first if you're driving the
pipeline; this file is meant to be pasted into the LLM session itself.

In one line: a Python script (`scripts/generate_corpus.py gen-specs`) emits
one JSON spec per record with full metadata + `creation_date` but no `text`;
you hand the LLM a batch of specs and this prompt; it returns
`{id, text, sentence_count}` per record; another Python step
(`scripts/generate_corpus.py merge`) joins specs + texts into the final
YAML. The deterministic part — distributions, code assignments, trend
shape — is unaffected by anything the LLM does or hallucinates.

---

## Prompt (paste verbatim; attach `fixtures/coding_framework.json`)

You are writing realistic English-language community-feedback records for a
humanitarian-response benchmark. The dataset is COVID-19-era feedback
collected by Red Cross / Red Crescent operators in West and Central Africa.
You are not anonymising or paraphrasing real data — every record is
synthetic.

### Inputs

You receive a JSON array `specs`, where each element is one record's full
metadata:

```json
{
  "id": "doc-0001",
  "metadata": {
    "dataset": "COVID-19",
    "feedback_type": "Observation, perception or belief",
    "region": "Western Area",
    "country": "Sierra Leone",
    "language": "en",            // always "en" in this corpus
    "source": "hotline",
    "year": 2020,
    "sensitive": false,
    "codes": "covid-19:observation-perception-or-belief:...:..."
  },
  "_context": {
    "code_name": "Belief that COVID-19 is caused by 5G",
    "code_description": "...",
    "code_examples": ["...", "..."],
    "trend_role": "spike|emerging|declining|step|cross_code|rumour|baseline"
  }
}
```

The `_context` block is **for your reading only** — do not echo it back. It
tells you which leaf the record codes for, what that code means, a few
real-world example utterances, and which planted trend (if any) the record
belongs to.

### Output

Return a JSON array `texts` of the same length and in the same order as
`specs`. Each element is:

```json
{
  "id": "doc-0001",
  "text": "Caller, a 58-year-old woman from Freetown, reports that…",
  "sentence_count": 3
}
```

Emit **only** the JSON array. No prose, no preamble, no markdown fences.

### Style guide

The reference register is operator-paraphrase casework notes — the same
register a hotline operator types into a CRM after a call. It is not survey
prose, not a focus-group quote, and not first-person diary writing (except
where the `source` field says otherwise; see below).

- **Length.** 100–500 characters, median ~200. Two to four sentences for
  most records. A small minority (< 5 %) may run longer for complex cases.
- **Structure.** Most records have two short paragraphs separated by a
  newline:
  1. *Caller situation & request* — demographics inline, request paraphrased.
     `"Woman, 62, IDP from Bo District, reports that her neighbours believe
      the vaccine causes infertility."`
  2. *Operator action / referral* — what was offered, explained, or who the
     caller was referred to. `"Operator clarified WHO guidance and provided
     hotline number for district health office."`
- **Voice by `source`**:
  - `hotline`, `outbound call`, `email`, `web form` → third-person operator
    paraphrase. Compact, clinical. ~80 % of the corpus.
  - `survey` → short third-person summary of a respondent's answer.
  - `focus group`, `community feedback session`, `volunteer report` →
    third-person summary, but may quote one short utterance ("…said: 'we
    don't trust the new vaccine'…"). Quoted utterances should appear in
    ~10 % of records overall.
- **Acronyms.** Casework notes are dense with acronyms — *use them*: WHO,
  IFRC, MSF, CDC, MoH, ETC, CHW, IDP, RC (Red Cross), PPE, CHS, RCCE.
  Expand on first use only when natural.
- **Realistic PII.** This corpus is the **input** to a downstream
  anonymisation step; it must give the anonymiser something to detect.
  Include realistic names (regionally plausible — Mariama Conteh, Jean-Paul
  Mukendi, not "John Smith"), village/neighbourhood names, phone numbers
  (`+232 76 543 210`, `+243 81 234 5678`), facility names ("Connaught
  Hospital", "Goma Health Centre"). **Do not** use placeholders like
  `[NAME]` or `[ADDRESS]`; write the names out. The downstream Presidio
  port is what redacts them.
- **Language.** Always English. `metadata.language` will be `"en"` for every
  record in this corpus; if you ever see anything else, write the prose in
  English anyway and ignore the field.
- **Code fidelity.** The `text` must be a *plausible feedback entry that
  would be coded under the given leaf*. Re-read `_context.code_name` and
  `_context.code_description` for each record. Treat `_context.code_examples`
  as canonical surface realisations: your prose should sit in the same
  semantic neighbourhood without parroting the examples verbatim.
- **Sensitive content.** If `metadata.sensitive` is `true`, the prose must
  warrant the flag — mention of death, identifiable household illness,
  GBV, child safeguarding, etc. Keep it factual, not graphic.
- **Comma-separated `codes`.** If `metadata.codes` lists multiple
  colon-paths (comma-separated), the prose must legitimately cover *all* of
  them. Use a slightly longer record (~300–500 chars) in that case.
- **Trend-role neutrality.** Do **not** mention the `_context.trend_role`
  in the prose. Trends are encoded only by which dates the spec
  assigned — your job is to write text that, taken on its own, looks
  organically generated. A `rumour`-role record should not say "rumour".
- **No template clones.** Vary opening verbs across the batch: *Caller
  asks…*, *Beneficiary requests…*, *Woman, IDP, reports that…*,
  *Family member enquires whether…*, *Mother of three calls about…*,
  *Operator follows up on previous outbound about…*

### Self-check before emitting

For each output element:

1. `text` runs 100–500 characters and ~2–4 sentences (or longer if `codes`
   is multi-valued).
2. `sentence_count` equals the actual number of sentences in `text`.
3. The prose plausibly fits under the leaf code in `_context`.
4. Voice matches `metadata.source` per the rules above.
5. PII shape matches the rule (realistic names, no placeholders).
6. No mention of `trend_role` or any internal pipeline metadata.

Begin emitting the JSON array now.

---

## Merge step (after all batches return)

Concatenate the per-batch `texts` arrays into a single JSONL file
(`texts.jsonl`, one `{id, text, sentence_count}` per line), then run:

```bash
uv run python scripts/generate_corpus.py merge \
    --specs fixtures/analyze_corpus.specs.jsonl \
    --texts texts.jsonl \
    --output fixtures/analyze_corpus.yaml
```

The merge script joins on `id`, validates that every spec has a matching
text and that `sentence_count` is plausible (warns if mismatched by more
than ±1), prepends the trend-benchmark comment block, and writes the final
YAML. The trend-plot PNG is re-generated alongside.
