## Spec

**What:** Make `POST /v1/assign-codes` always return at least one `assigned_codes` entry
when no code is applied, carrying a human-readable explanation that leads with
`NO CODING APPLIED.`

Three code paths currently end with no code assigned, and only one of them explains itself:

| # | Trigger | Today returns | Explanation? |
|---|---|---|---|
| 1 | Empty `content` — `src/qfa/api/routes.py:424` (issue #138) | `[]` | none |
| 2 | Every candidate below `confidence_threshold` — `src/qfa/services/orchestrator.py:1367` | one null entry | combined rejections, unlabelled |
| 3 | Nothing selected at any level — `src/qfa/services/orchestrator.py:1374` | `[]` | none |

Cases 1 and 3 leave EspoCRM with nothing at all to display: `4_1_assign_codes.php` reads
`assigned_codes.0.explanation`, gets null, and still sets `autoCodingStatus = 'completed'`.
Case 2 does deliver text, but it is unlabelled and hard to read.

This ticket unifies all three behind one contract and reformats the message.

**Why:** Users see codes silently missing in EspoCRM with no indication why (parent #255).
Case 2's explanation already reaches Espo via `autoCodingExplanation`, so improving that text
is immediately visible to users without any EspoCRM change. Cases 1 and 3 send nothing, so no
amount of formatting helps until they also return an entry.

### Target format (case 2)

```
NO CODING APPLIED.
No code reached the 10% confidence threshold, so this record needs human review.

Shelter > Repairs > Roofing — 4%
  No mention of roof damage; the feedback concerns rent costs.

Water — 3%
  No reference to water access.

Food Security — 2%
  Concerns housing, not food.

5 further codes scored below 2%.
```

Cases 1 and 3 use the same `NO CODING APPLIED.` lead with a case-specific second line and no
candidate list.

### Notes for the implementer

- `_combine_rejected_explanations` (`orchestrator.py:265`) currently takes only `rejected`.
  The threshold is available at the call site (`orchestrator.py:1370`) and must be threaded in.
- A rejected candidate has exactly one sub-threshold level — the traversal `continue`s on the
  first failure (`orchestrator.py:1503-1508`). So the last accumulated score is always the
  minimum, and the decisive level is unambiguous.
- When `confidence_threshold` is `None` nothing is ever rejected, so case 2 cannot occur
  without a real threshold value. The percentage in the lead line always has a value.
- English only. `ApiAssignCodesRequest` has no `output_language` field and this ticket does not
  add one; localization is explicitly out of scope.
- Keep the message in the existing `explanation` string. Do **not** add a `pretty_output`
  property to `ApiAssignCodesResponse` — that would require a matching EspoCRM script change
  and is out of scope here.

## Acceptance criteria

- [ ] `assigned_codes` is never an empty list; when no code is applied it contains exactly one
      entry with null `coding_level_*` / `confidence_*` fields and a non-empty `explanation`
- [ ] Every no-code `explanation` begins with the literal line `NO CODING APPLIED.`
- [ ] Case 1 (empty `content`) yields an explanation stating the feedback text was empty;
      `tests/api/test_routes.py::test_assign_codes_empty_content_returns_no_codes` is updated
      from asserting `== []` to the new shape
- [ ] Case 2 (below threshold) yields a lead line naming the threshold as a whole percentage
- [ ] Case 3 (no candidate selected) yields an explanation stating that no code in the
      framework was judged relevant
- [ ] Confidence values render as whole percentages, not `0.00`–`1.00` decimals
- [ ] Each listed candidate shows only its decisive (final, lowest-scoring) level's judge
      explanation, not one line per hierarchy level
- [ ] At most 3 rejected candidates are listed, highest-scoring first, followed by a single
      line counting the remainder; that count line is absent when 3 or fewer were rejected
- [ ] `_combine_rejected_explanations` receives the confidence threshold from its call site
- [ ] Docstrings in `routes.py` and `orchestrator.py` describing the #138 empty-list contract
      are reworded to match the new behaviour
