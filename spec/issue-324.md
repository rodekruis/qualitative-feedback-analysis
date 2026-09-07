## Context

`PresidioAnonymizer._get_unique_id` (`src/qfa/adapters/presidio_anonymizer.py:122-135`)
numbers placeholders off the length of the mapping it is given:

```python
placeholder = f"<{entity_type}_{len(mapping.keys())}>"
```

That `mapping` is created fresh inside every `anonymize()` call, so **each call
restarts numbering at 0**. `LLMCallExecutor.anonymize_records`
(`src/qfa/services/llm_call_executor.py:107-126`) calls `anonymize()` once per
record and then merges the per-record mappings:

```python
for record in records:
    redacted, mapping = self._anonymizer.anonymize(record.content)
    merged.update(mapping)   # line 123 — collisions silently overwrite
```

Because every record's mapping starts at index 0, distinct records produce
identical placeholder keys for different original values, and `dict.update`
keeps only the last one. The earlier value is lost, and the *surviving* value is
then substituted back into the earlier record's text by `deanonymize`.

`analyze_hierarchical` uses this path (`src/qfa/services/analyze.py:391`), so
this is live today.

### Reproduction

Run against the real `PresidioAnonymizer`:

```python
r1 = "Olena Kovalenko reported the water point in Kharkiv ran dry."
r2 = "Piet Jansen reported the water point in Utrecht ran dry."
red1, m1 = a.anonymize(r1)
red2, m2 = a.anonymize(r2)
merged = {}; merged.update(m1); merged.update(m2)
a.deanonymize(red1, merged)
```

Observed:

```
record 1 -> <ORGANIZATION_1> reported the water point in <LOCATION_0> ran dry.
              mapping {'<LOCATION_0>': 'Kharkiv', '<ORGANIZATION_1>': 'Olena Kovalenko'}
record 2 -> <PERSON_1> reported the water point in <LOCATION_0> ran dry.
              mapping {'<LOCATION_0>': 'Utrecht', '<PERSON_1>': 'Piet Jansen'}

merged: {'<LOCATION_0>': 'Utrecht', '<ORGANIZATION_1>': 'Olena Kovalenko', '<PERSON_1>': 'Piet Jansen'}

de-anonymised record 1:
  "Olena Kovalenko reported the water point in Utrecht ran dry."
expected:
  "Olena Kovalenko reported the water point in Kharkiv ran dry."
```

`Kharkiv` silently became `Utrecht`.

### Impact

Hierarchical analyse output can re-attribute statements to the wrong location,
organisation, or person. It fails **silently** — no error, no log line, and the
output is well-formed prose, so it is unlikely to be caught by review of the
response.

Scope note: the merged mapping is per-request, and all records in a request
belong to one tenant, so this is a **data-integrity** bug rather than a
cross-tenant disclosure. Labelled `bug`, not `security`, on that basis — worth
a second opinion during triage.

## Task

Thread a **shared mapping and counter** through per-record anonymisation so
placeholders are unique across all records in a request.

Points to settle during refinement:

- `_get_unique_id` derives the index from `len(mapping)`, which conflates "next
  index" with "map size". A shared, explicit counter is clearer than relying on
  the caller passing one accumulating dict.
- The existing dedup loop in `_get_unique_id` (reuse a placeholder when the same
  original value recurs for the same entity type) should keep working across
  records — the same person named in two records should get **one** placeholder.
  That is a behaviour improvement for cross-record analysis, not just a fix.
- Check whether any prompt text or test fixture depends on placeholder numbering
  restarting per record.

This also unblocks #325 — per-record anonymisation is the fix for the
event-loop stall there, but it cannot be adopted in `analyze_bulk` until
placeholders are collision-free.

## Acceptance criteria

- [ ] Anonymising N records yields a mapping in which every placeholder maps to
      exactly one original value.
- [ ] `deanonymize` round-trips each record's redacted text back to its original
      content when given the merged mapping.
- [ ] The same original value appearing in two different records maps to the
      same placeholder.
- [ ] Regression test covering the two-record collision in the reproduction
      above (distinct `LOCATION` values across records).
- [ ] `analyze_hierarchical` verified end to end on a multi-record corpus with
      repeated and distinct entities.
