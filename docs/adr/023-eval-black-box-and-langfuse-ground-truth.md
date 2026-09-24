# ADR-023: `eval/` as a black-box client, and Langfuse as the analyze ground truth

## Status

Accepted

## Context

ADR-022 left two questions open: where `eval/` sits in the architecture, and where the ground truth for the analyze feedback pool lives. #391, the analyze eval pool ticket, needed answers to both. Building its upload tools raised a third question, about how a shared dataset changes once other work depends on it. This ADR answers all three.

`eval/` already exists as an external test harness. It scores a live endpoint over HTTP, the same way any other client calls the service. The analyze pool adds three new Langfuse datasets: 10 analyst prompts, about 110 synthetic feedback records, and a keyword vocabulary. A separate piece of work, #392, reads all three. It builds its own test cases from them, and computes the right answer to each of the 10 prompts from the labels and the keywords. It then scores a live analyze-bulk response against that computed answer.

## Decision

1. **`eval/` stays a black-box HTTP client outside the hexagon.** It calls the deployed service the same way any real client does: over HTTP, with the same authentication, and the same request and response shapes. It never imports `qfa`.

   The `import-linter` "Enforce hexagonal layers" contract only covers the `qfa` package. It cannot catch a `qfa` import inside `eval/`, so a reviewer must catch that by reading the code. Code that genuinely needs `qfa` lives in `scripts/` instead. One example is `scripts/check_pool_masking.py`, which runs the same anonymizer the service runs, to check whether it hides a word the eval data needs intact.

2. **The ground truth for analyze is the labels and the keywords stored in Langfuse, not a hand-written answer key.** Every record in `feedback/records-en-v1` carries the facts a person already knows about it: its theme, an unmet need it reports, which vulnerable group it is about, and more. The person who writes the record's text sets these facts at the same time, never afterwards. `feedback/vocab-v1` gives each label value the words that show a model's answer named it.

   #392 reads both datasets to build its test cases and to compute each prompt's right answer. No file in this repository holds a copy of that answer.

3. **A dataset changes in place.** An upload updates only the items whose content changed, and creates any new ones. Langfuse keeps every old version of a changed item, so an update never loses data. A new dataset name is reserved for a real break. A real break is a different language, a different setting, or a different item shape that an existing reader cannot handle the same way. Work that depends on one state of a dataset records which version it used, instead of the dataset gaining a new name for every edit.

## Options considered

### An answer key written by hand (rejected)

A person writes, in a separate file, what counts as the right answer to each prompt. This drifts from the pool as records change. Nobody can check that it is still correct without doing the same reading again.

### The existing COVID corpus, with no answer key (rejected)

Reuse `fixtures/analyze_corpus.yaml`, already about 5,000 records, and accept that most prompts find little or nothing planted in it. A low score against an empty answer key says nothing about the model. There is nothing there for it to have missed.

### A copy of the data in `fixtures/` (rejected)

Keep a second copy of the records and the vocabulary as a fixture file, next to the Langfuse copy. Two copies drift apart. Each Langfuse change needs a matching `fixtures/` update, and nothing catches a mismatch until a run behaves oddly.

### A new dataset name for every edit (rejected)

Publish `feedback/records-en-v1`, then `-v2`, then `-v3`, and so on, each time the text changes, instead of updating in place. Langfuse already keeps each item's history inside one dataset, so a new name adds nothing. It also spreads runs across many dataset names, which makes two runs harder to compare.

## Consequences

- `eval/upload_prompts.py` and `eval/upload_pool.py` import nothing from `qfa`. `scripts/check_pool_masking.py` does, and lives outside `eval/` for that reason.
- `feedback/records-en-v1`, `feedback/vocab-v1`, and `analyze/prompts-v1` are the three Langfuse datasets a future evaluation script reads. No local fixture copy needs to stay in sync with them.
- An upload script is safe to run again. It skips unchanged items, updates changed items with the old version kept, and deletes nothing.
- A dataset name changes only for a language, a setting, or an item shape that an existing reader cannot handle the same way as before.

## When to revisit

- If a future evaluation script needs to compare scores across two item versions side by side, check whether one dataset, versioned in place, still gives an easy way to do that.
- If `scripts/` grows enough `qfa`-importing code to look like a second, uncontrolled hexagon boundary, add it to the `import-linter` configuration too.

## Participants

Inês
