# ADR-017: Replace Mistral-medium with GPT-5.4 for code assignment

## Status

Accepted

## Context

The `qfa-dev` backend assigns codes to humanitarian feedback records via
`LiteLLMClient`, using `mistral-medium-2505` on Azure AI Foundry
(`azure_ai/mistral-medium-2505`). Mistral was originally chosen for privacy
reasons.

A recent benchmark paper, ["Can Large Language Models Reliably Code Qualitative
Humanitarian Data?"](https://arxiv.org/abs/2606.26541), evaluates 46 LLMs
against a human Gold Standard on humanitarian transcripts using inter-rater
reliability and discrepancy analysis. Our reading is that Mistral scores
dramatically below top models — including GPT-5.4.

## Decision

Move the LLM backend from `mistral-medium-2505` to `gpt-5.4`,
accepting the higher per-call cost as justified by the reliability gain shown
in the benchmark above.

## Options Considered

### Option A: Keep Mistral-medium (rejected)

- **Pro**: No cost increase, no change to data-handling posture.
- **Con**: Benchmark shows materially lower reliability on the pipeline's core
  task, worst on the highest-stakes categories (safety, discrimination).

### Option B: Move to GPT-5.4 on Azure Foundry (chosen)

- **Pro**: Benchmark-supported reliability improvement on humanitarian coding.
- **Pro**: Same Azure Foundry resource/region (Sweden Central), so the
  data-residency boundary is largely preserved.
- **Con**: Higher per-call cost.
- **Con**: Privacy concerns

## Consequences

- Per-record cost increases; track against real usage before wider rollout.
- Privacy rationale needs re-validation: confirm Azure OpenAI's data-processing
  terms on this Foundry resource still meet the requirements Mistral was
  chosen to satisfy.
- `LLM_API_BASE`, `llm_model`, and `AZURE_API_VERSION` (if applicable) need
  updating in all environments, not just `qfa-dev`.

## When to revisit

- If Azure Foundry GPT-5.4 capacity or regional availability proves
  insufficient for production load, fall back to evaluating the next
  best-performing model in the benchmark rather than reverting to Mistral.
- If a future model closes the reliability gap at Mistral's cost/privacy
  profile, re-run this comparison — this decision is a snapshot against the
  cited benchmark, not a permanent ranking.

## Participants

Olaf, Jacoppo, Daan, Marius (in the teams channel).