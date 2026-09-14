# ADR-021: Two-model NER union for person-name recall

## Status

Accepted

## Context

`PresidioAnonymizer` picks one spaCy model per text, by detected language,
and analyses the text once. `en_core_web_sm` (and the other per-language
models) are trained on a distribution that misses names common in this
service's population two ways: missed entirely (never tagged, so the name
reaches the LLM and the analyst verbatim), and mislabelled as a non-PERSON
type (e.g. `ORGANIZATION`) — which `AnalyzeService` then restores before
the analyst sees it, since only `PERSON` placeholders are withheld
(`docs/architecture/06-prompt-envelope.md`). Both were reproduced with
real examples.

`xx_ent_wiki_sm` — the multilingual fallback model, already a dependency
and already loaded in memory for unsupported languages — is not *better*
at this than the per-language models, but it is complementary: it catches
names the per-language model misses, and misses some the per-language
model catches.

## Decision

For every text whose detected language isn't already `xx`, run a second,
`xx`-model, `entities=["PERSON"]` pass and union it onto the first:
`PERSON` wins any overlap — a non-`PERSON` result overlapping a `PERSON`
span is dropped, and a `PERSON` span from the second pass that doesn't
overlap an existing one is added. A name only the per-language model
catches is untouched — this is additive, never a replacement.

Separately, `AnalyzeService._ANALYZE_RETAINED_PLACEHOLDER_TYPES` widens
from `{"PERSON"}` to `{"PERSON", "NRP"}`: `NRP` (nationality/religious/
political group) is where misread given names land. `ORGANIZATION` stays
restored so findings keep useful context.

Both changes live in `qfa.adapters.presidio_anonymizer`, so every use case
(analyze, summarize, coding, sensitivity) gets the recall improvement, not
just analyse.

## Consequences

- Adds one more `analyze_iterator` call per non-`xx` text, parallelised
  across the same pool as the primary pass (see
  [ADR-003's amendment](003-fully-async-concurrency.md#amendment-2026-09-07-thread-offload-for-blocking-work)).
  Measured cost is small relative to the primary pass, since that's the
  one spaCy's per-token pipeline stages dominate.
- False positives rise slightly: `xx_ent_wiki_sm`'s spans are sloppier and
  sometimes swallow an adjacent common noun into the person span. In
  `analyze` output — where `PERSON` isn't restored — that word disappears
  along with the placeholder. Acceptable, but visible.
- No on/off switch for the second pass. A knob to *lower* PII recall is a
  footgun; recall-first is the right default for this data.

## Limitations

This reduces false negatives; it does not eliminate them. NER is
statistical — some names will still slip through, including ones this
union approach does not catch (both passes can miss, or both can mislabel,
the same name). Any claim stronger than "reduces" is wrong.
