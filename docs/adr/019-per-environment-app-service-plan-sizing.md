# ADR-019: Per-environment App Service plan sizing, prd on P0v3

## Status

Accepted

## Context

On the B2 plan the App Service sat close to its memory ceiling while serving
several analysis calls concurrently (evidence: the Azure portal memory graph
attached to issue #204). The instance has not been sized since it was first
provisioned, and the question was whether to buy headroom or to shrink the
footprint.

What holds memory in this service:

- The **BGE-M3 ONNX embedder is baked into the image** and, once loaded, stays
  resident for the process lifetime ([ADR-014](014-embedding-port-and-self-hosted-model.md),
  `src/qfa/adapters/embedding.py`).
- The **per-request corpus** plus per-batch tokeniser padding — one
  `session.run()` per batch of `EMBEDDING_BATCH_SIZE` records (default 100).
- `entrypoint.sh` execs `gunicorn … --worker-class asgi` with **no `-w`**, so
  there is exactly **one worker** and therefore one resident ONNX session per
  instance. Concurrency inside that worker is async, so concurrent calls share
  the model but each add their own corpus.

The relevant tiers:

| SKU | vCPU | RAM |
|---|---|---|
| `B2` (Basic) | 2 | 3.5 GiB |
| `P0v3` (Premium v3) | 1 | 4 GiB |
| `P1v3` (Premium v3) | 2 | 8 GiB |

`P0v4` was not offered in the environment's region at decision time.

## Decision

Size the App Service plan **per Terraform workspace**: `prd` runs **`P0v3`**,
`dev` and `staging` stay on **`B2`**.

The values live in `var.app_service_plan_sku_by_env` (`infra/variables.tf`) and
are resolved by `local.app_service_plan_sku`, which `lookup`s the current
workspace and **falls back to `B2`** for any workspace not in the map — a new
environment can never be silently provisioned as Premium.

`P1v3` is held as the **documented escalation**, not applied now.

## Options Considered

### A. prd on P0v3, dev/staging on B2, P1v3 held in reserve (chosen)

- **Pro**: Buys headroom exactly where user load is, at the smallest Premium
  step; cost rises on one environment only.
- **Pro**: Reversible in one word, and the next step up is already decided, so
  an alert firing turns into an edit rather than a fresh investigation.
- **Con**: Trades vCPU for RAM (see Consequences) — the non-obvious cost.
- **Con**: dev no longer mirrors prd sizing.

### B. Stay on B2 and shrink the footprint (rejected)

Lower `EMBEDDING_BATCH_SIZE`, cap request concurrency, or stop baking the model
into the image and load it on demand.

- **Pro**: No cost increase.
- **Con**: Each lever trades throughput or latency for headroom, and none of
  them moves the ceiling — the same graph reappears at higher load.
- **Con**: On-demand model loading turns a one-off startup cost into a
  per-request one, against [ADR-014](014-embedding-port-and-self-hosted-model.md).

### C. Upgrade every environment uniformly (rejected)

- **Pro**: dev reproduces prd's resource behaviour.
- **Con**: dev and staging carry no user load — staging sees smoke tests only.
  Pure cost for headroom nothing will use.

### D. Go straight to P1v3 (rejected)

- **Pro**: Fixes both axes at once — 2 vCPU *and* 8 GiB.
- **Con**: Premature ahead of real production traffic. Size on measurements
  from actual users rather than on a pre-launch guess; the alerts below are the
  measurement.

## Consequences

- **prd RAM 3.5 → 4 GiB, but prd vCPU 2 → 1.** `P0v3` is the smallest Premium
  v3 tier and has *fewer* cores than `B2`. Startup — Alembic migrations plus
  loading the embedding model — is slower on prd than on dev, and more likely
  to trip the 80% `high_cpu` alert.
- Higher run cost on prd only; dev and staging are unchanged, so applying this
  to those environments is a no-op.
- **dev no longer mirrors prd sizing**, so a prd memory ceiling cannot be
  reproduced in dev.
- Changing the tier **restarts the app** — App Service moves the site to new
  workers, so the container re-migrates and reloads the model. See the runbook
  in [Operational how-tos](../operations/how-to.md).
- **Escalation trigger:** if `high_memory` or `high_cpu` keeps firing on prd
  once real users are on it, move `prd` to `P1v3` (2 vCPU / 8 GiB) — a one-word
  edit to `var.app_service_plan_sku_by_env` plus an apply. Do **not** raise the
  alert thresholds instead.

## When to revisit

- The escalation trigger above fires.
- `P0v4` becomes available in the region — it is the same class at a newer
  hardware generation.
- The worker count stops being 1, or the model stops being baked into the
  image: both change the memory floor this sizing assumes.

## Participants

Marius, Daan
