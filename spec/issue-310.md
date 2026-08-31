## Summary

`CodingService.assign_codes` (`src/qfa/services/coding.py`) does not use the
judge/primary LLM split introduced by #258 — both the one-shot code pick and
the per-level judge run on the **primary** connection, never on
`JUDGE_LLM_MODEL`. This is documented as deliberate in the module docstring
(`coding.py:23-26`) and pinned by
`tests/services/test_orchestrator_judge_routing.py`, quoting:

> Both the pick and the per-level judge run on the **primary** LLM
> connection. The judge/primary split introduced by #258 deliberately
> excludes this path, so the service takes no judge client at all.

Filed as a follow-up from #309 (grill-me on that issue confirmed the ask:
extend the split here rather than leave it out of scope).

## Ask

Extend `JUDGE_LLM_*` routing to `CodingService`'s per-level judge calls
(`_judge_code_level` / `_judge_selected_path`, `coding.py:352-428`), matching
the pattern already used by `AnalyzeService` and `SummarizeService`
(`judge_llm: LLMPort | None = None` constructor param, defaulting to `llm`
when unset — see `analyze.py:149-158` for the reference implementation).

The one-shot code **pick** itself is a separate question — it's not framed
as an LLM-as-judge call in the existing code, so whether it should move to
the judge connection too needs a decision, not an assumption. Default to
leaving the pick on the primary connection and only routing the per-level
judge, unless someone has reason to do otherwise.

## Why this was excluded originally

Worth reading before implementing: `docs/adr/020-mistral-medium-as-judge-model.md`
(the decision to point the judge connection at `azure_ai/mistral-medium-3-5`)
does not mention `assign_codes` at all — its four call sites are the
`analyze` judge, the hierarchical leaf judge, and the two `summarize` judges.
Check `git log`/PR history around #258 for why `assign_codes` was scoped out,
in case there's a reason beyond "not needed for that PR" (e.g. latency
budget, a coding-specific accuracy concern with a cheaper judge model).

## What needs to change

1. Add `judge_llm: LLMPort | None = None` to `CodingService.__init__`,
   defaulting to `llm` (same inherit-when-unset pattern as `analyze.py` /
   `summarize.py`).
2. Route `_judge_code_level`'s LLM call through `self._judge_llm` instead of
   `self._llm`.
3. Wire it in composition (`qfa.api.composition`) alongside the existing
   `resolve_judge_llm_settings` wiring for the other two services.
4. Update `tests/services/test_orchestrator_judge_routing.py` — it currently
   pins the *exclusion*; it needs to pin the new inclusion instead, plus a
   case covering the inherit-when-`JUDGE_LLM_MODEL`-unset default.
5. Update the `coding.py` module docstring (currently states the exclusion
   as settled fact) and `docs/adr/020-mistral-medium-as-judge-model.md`
   (currently lists only four call sites) to reflect the new scope.

## Related

- #309 — the analyze-bulk judge 400 bug this was split out of.
- #299 — separate, already-tracked gap in the two `summarize` judge sites'
  error handling. Not the same bug, but same general "judge call sites
  aren't uniformly implemented" theme.
- ADR-020 (`docs/adr/020-mistral-medium-as-judge-model.md`).
