BLOCKED-check done: every file the spec's decision touches (`infra/app_service.tf`, the `dev`/`staging`/`prd` workspace model, the alert rules) exists in this tree. The spec is buildable. Plan follows.

---

# Implementation plan — issue #204

Verified against this worktree (`43d6b5c`, freshly based on `origin/main`). The spike's question is already answered in the spec — upgrade, `prd` → P0v3, `dev` stays B2 — so the build is a Terraform sizing change plus the decision record that discharges the acceptance criterion. `infra/app_service.tf:10` hardcodes `sku_name = "B2"`; nothing else in the tree pins a tier except one stale comment (`infra/observability.tf:160`). No Python is touched.

Two facts about this repo shape everything below:

- **`terraform` is not installed in this worktree** (`which terraform` → not found), and there is no `terraform test`/`fmt` step in any workflow. The builder cannot run `validate`, `plan`, or `terraform test` locally. The parse-and-diff proof is CI's existing plan job; local proof is grep/`git diff` plus `make docs`.
- **CI's automatic `terraform plan` runs against the `dev` workspace only** (`docs/operations/release-flow.md:108`), and this change is a deliberate no-op on `dev`. So the PR's own plan output will show **no diff on the service plan** — that is the expected result, not a failure, and the PR body has to say so or a reviewer will read it as "the change did nothing".

---

## Decisions the spec left open

1. **Per-env SKU lives in a workspace-keyed map in `infra/variables.tf`, not in a `TF_VAR` passed per GitHub environment.** Every other per-env difference in this repo is either derived from `terraform.workspace` (`infra/locals.tf:2`) or supplied as a `TF_VAR_*` from GitHub environment variables in `.github/workflows/terraform.yaml`. The `TF_VAR` route would put production's tier in GitHub settings, outside code review, and a missing variable would silently resolve to the default — i.e. prd quietly downgrades. A map keyed by workspace keeps all three values reviewable in the PR diff and needs **no** GitHub configuration, so `.github/workflows/terraform.yaml` is not touched at all. A bare `local.env == "prd" ? "P0v3" : "B2"` was the alternative; rejected because it leaves `staging`'s tier implicit, and the escalation the spec anticipates (P1v3) should be a one-word edit next to a description that says what the tiers are.

2. **`staging` stays B2.** The spec names only prd and dev. `staging` exists as a workspace and a GitHub environment (`terraform.yaml` choice list, `docs/operations/how-to.md:14`). It carries smoke-test traffic, not user load, so it stays on the cheap tier; the map states this explicitly rather than leaving it to a fallback.

3. **Unknown workspaces fall back to B2.** `lookup(..., local.env, "B2")` — a future workspace provisions cheap by default and can never silently create a Premium plan.

4. **P0v3 halves the vCPU count while adding only 0.5 GiB of RAM.** B2 is 2 vCPU / 3.5 GiB; P0v3 is 1 vCPU / 4 GiB; P1v3 is 2 vCPU / 8 GiB. So the signed-off change buys ~14% more memory and gives up half the CPU — and `infra/observability.tf:160` already records that embedding-model load spikes CPU at startup on 2 vCPU. This was decided with Daan and is not for the builder to relitigate: implement P0v3. But it must be *written down* — the ADR records the trade-off, and the escalation trigger to P1v3 (which fixes both axes: 2 vCPU / 8 GiB) is tied to the existing `high_cpu` / `high_memory` alerts so the deferred half of the spike is operational rather than folklore. **Do not** change alert thresholds in this PR; one variable at a time.

5. **No new Terraform output, no `.tftest.hcl`.** With no `terraform` binary and no test step in CI, a test harness would be unrunnable by the builder and unrun by CI. Post-apply verification is `az appservice plan show --query sku.name`, which checks Azure rather than state, and lands in the runbook (step 4).

6. **One line of pre-existing doc rot gets fixed:** `docs/operations/deployment.md` § *Container lifecycle* step 2 says `uvicorn qfa.main:app …`; `entrypoint.sh` actually execs `gunicorn qfa.main:app --worker-class asgi` with no `-w`, i.e. one worker. That line sits directly below the sizing table this PR adds, and "one worker → one resident ONNX session" is the whole memory model the sizing rests on, so leaving it stale would make the new table read as contradicting the page.

7. **`docs/security-brief.html` is not touched.** `AGENTS.md` requires it when anything security-related changes; a plan tier changes no boundary, identity, secret path, or data flow. Stated here so the builder doesn't hunt for something to edit.

**Explicitly out of scope** (do not do these): change `var.postgres_sku_name`; add `worker_count`, `zone_balancing_enabled`, or `per_site_scaling_enabled`; add autoscale rules; change alert thresholds or windows; add a gunicorn `-w` flag or touch `EMBEDDING_BATCH_SIZE`; edit any file in `.github/workflows/`; hand-edit `project.version` or write a changelog (python-semantic-release owns those — see `docs/operations/release-flow.md`); edit `spec/issue-204.md`.

---

## Step 1 — Terraform: per-environment plan SKU

**Files:** `infra/variables.tf`, `infra/locals.tf`, `infra/app_service.tf`, `infra/observability.tf`

1. `infra/variables.tf` — insert a new section between the `# --- App configuration (non-secret) ---` block and `# --- PostgreSQL configuration ---`, so the two infra-sizing knobs (`app_service_plan_sku_by_env`, `postgres_sku_name`) are adjacent:

   ```hcl
   # --- App Service plan sizing ---

   variable "app_service_plan_sku_by_env" {
     description = "App Service plan SKU per Terraform workspace. A workspace missing from this map falls back to B2 (see locals.app_service_plan_sku), so a new environment is never silently provisioned as Premium. prd runs P0v3 because B2 ran close to its memory ceiling under concurrent API calls; see ADR-019."
     type        = map(string)
     default = {
       dev     = "B2"
       staging = "B2"
       prd     = "P0v3"
     }
   }
   ```

   Casing matters: the azurerm provider's accepted value is `P0v3` (not `P0V3`). `terraform fmt` aligns the `=` of consecutive single-line attributes only, so `description`/`type` align and `default = {` is left unpadded — write it exactly as above, since no HCL formatter runs in CI or pre-commit to fix it later.

2. `infra/locals.tf` — add one entry after `db_aad_principal_name` (line 9) and before the `# Resource IDs for shared infra.` comment block:

   ```hcl
   # Premium (Pv3) only where user load justifies it — ADR-019.
   app_service_plan_sku = lookup(var.app_service_plan_sku_by_env, local.env, "B2")
   ```

   The existing alignment column is set by `managed_identity_name` / `db_aad_principal_name` (21 chars); `app_service_plan_sku` is 20, so **no existing line is re-aligned** and the diff stays additive.

3. `infra/app_service.tf:10` — `sku_name = local.app_service_plan_sku`. Argument names in the block are unchanged, so the alignment column of the `azurerm_service_plan.main` block does not move; the diff is one line.

4. `infra/observability.tf:159-160` — the comment above `azurerm_monitor_metric_alert.high_cpu` currently reads "On a B2 (2 vCPU), the embedding model loading spikes CPU at startup." Replace with a comment that states the tier is now per-environment (B2 = 2 vCPU on dev/staging, P0v3 = 1 vCPU on prd), that the startup spike is therefore sharper on prd, and that repeated firing on prd is the documented trigger to move to P1v3 (2 vCPU / 8 GiB) — pointing at ADR-019. Threshold, window, frequency and severity are unchanged. Leave the `high_memory` block's comment as is; it is still accurate.

**Checks that prove this step** (all runnable in the worktree):

- `grep -n 'sku_name' infra/app_service.tf` → no literal `"B2"` remains in `app_service.tf`.
- `grep -rn '"B2"' infra/` → hits only `variables.tf` (the map defaults) — nothing else in `infra/` hardcodes a tier.
- `grep -n 'app_service_plan_sku' infra/locals.tf infra/app_service.tf infra/variables.tf` → the local is defined once, consumed once, and the variable is referenced only by the local.
- `git diff --stat infra/` → exactly the four files above; `git diff infra/` shows no re-alignment noise.
- **CI (automatic, on the PR):** the `Terraform` workflow's `changes` filter sees `infra/**` and runs `terraform plan` on `dev`. Two things must hold: the plan **succeeds** (this is the only syntax/validate proof available), and it reports **no change to `azurerm_service_plan.main`** — `dev` resolves to `"B2"`, identical to today's literal, and `lookup` over a known map yields a known string so there is no spurious diff.
- **prd diff (human, before apply):** run the `Terraform` workflow via `workflow_dispatch` with `environment: prd, command: plan`. Expected: exactly one change, `azurerm_service_plan.main` `sku_name: "B2" -> "P0v3"`, as an **in-place update (`~`)**. If it renders as a replacement (`-/+`), **stop and do not apply** — replacing the plan detaches and re-attaches the web app and turns a restart into a real outage. This instruction belongs in the PR body (step 6), not only here.

**Ordering:** first. Every doc step below names `var.app_service_plan_sku_by_env` and must match what actually landed.

---

## Step 2 — ADR-019: the decision record

**Files:** `docs/adr/019-per-environment-app-service-plan-sizing.md` (new), `docs/adr/index.md`

`019` is the next free number: `018-no-third-party-text-in-error-envelope.md` landed with #78 (commit `dbb5006`) and `008` is the only gap (superseded, under `obsolete/`). Follow the house structure of `016-guard-auto-deploy-on-publish.md`: `# ADR-019: <title>`, `Status: Accepted`, Context, Decision, Consequences.

Content it must carry — and nothing more:

- **Context.** Memory on B2 sat close to the ceiling under concurrent API calls (evidence: the portal screenshot in issue #204). What holds memory: the ONNX embedder is baked into the image and resident for the process lifetime (ADR-014, `src/qfa/adapters/embedding.py`), plus the per-request corpus and per-batch padding (`EMBEDDING_BATCH_SIZE`, default 100). One gunicorn worker (`entrypoint.sh`, no `-w`), so one resident session per instance.
- **Options considered, with why-not.** (a) Stay on B2 and shrink the footprint — lower `EMBEDDING_BATCH_SIZE`, cap request concurrency, or stop baking the model: trades throughput and latency for headroom and leaves the ceiling in place. (b) Upgrade every environment uniformly: dev and staging carry no user load; pure cost. (c) Go straight to P1v3: premature ahead of real production traffic (the spec's explicit reasoning). (d) **Chosen:** prd on P0v3, dev and staging on B2, with P1v3 held as the documented escalation. P0v4 was not available in the region at decision time.
- **Consequences.** prd RAM 3.5 → 4 GiB **and vCPU 2 → 1** — call the CPU reduction out plainly, it is the non-obvious cost; startup (migrations + embedding-model load) is slower and more likely to trip the 80% CPU alert. Higher run cost on prd only. dev no longer mirrors prd sizing, so a memory ceiling cannot be reproduced in dev. Changing the tier restarts the app (see step 4). **Escalation trigger:** if `high_memory` or `high_cpu` keeps firing on prd once real users are on it, move `prd` to `P1v3` (2 vCPU / 8 GiB) — a one-word edit to `var.app_service_plan_sku_by_env` plus an apply.
- State the tier specs as a small table (B2: 2 vCPU / 3.5 GiB · P0v3: 1 vCPU / 4 GiB · P1v3: 2 vCPU / 8 GiB). **Do not invent measured MB figures** — the only evidence in hand is the issue's screenshot; cite it, don't quantify it. Sanity-check the three tier rows against current Azure App Service pricing docs while writing; if Azure's published numbers differ from the ones above, the Azure numbers win and the SKU choice still stands (it was human-signed-off on the P0v3/P1v3 pair, not on my arithmetic).

`docs/adr/index.md`: add the row to the index table **and** the entry to the hidden `toctree`. `make docs` runs `sphinx-build -W`, so a file missing from the toctree fails the build.

**Check:** `make docs` builds clean; `grep -c '019-per-environment-app-service-plan-sizing' docs/adr/index.md` → 2 (table row + toctree).

**Ordering:** after step 1, before steps 3-5 (they link this ADR; `-W` turns a broken link into a build failure).

---

## Step 3 — Deployment doc: record the sizing

**File:** `docs/operations/deployment.md`

1. In § *Runtime topology* (after the sentence "one App Service plan per environment (`dev`, `staging`, `prd`)", before the mermaid block), add a short table and two sentences:

   | Environment | Plan SKU | vCPU | RAM |
   |---|---|---|---|
   | dev | `B2` | 2 | 3.5 GiB |
   | staging | `B2` | 2 | 3.5 GiB |
   | prd | `P0v3` | 1 | 4 GiB |

   Sentences: the values live in `var.app_service_plan_sku_by_env` (`infra/variables.tf`), resolved per Terraform workspace by `local.app_service_plan_sku`, with unknown workspaces falling back to B2; why prd differs and when to escalate → link ADR-019. Add the one added fact that a reader can't get from the table: the container runs a **single** gunicorn worker, so exactly one ONNX embedding session is resident — raising the worker count would multiply the memory floor, so it is not a way to spend the extra RAM.
2. § *Container lifecycle* step 2: `uvicorn qfa.main:app …` → `gunicorn qfa.main:app --worker-class asgi` (one worker), matching `entrypoint.sh`. Keep the following trade-off sentence about start time; it still holds. (Decision 6.)
3. Changing the tier is a restart, not a zero-downtime operation — one clause, pointing at the runbook added in step 4. Do not duplicate the runbook here.

**Check:** `make docs` clean; `grep -n 'uvicorn' docs/operations/deployment.md` → no match; the page grows by roughly the table plus three sentences (AGENTS.md: a page must not get longer unless behaviour was added — here it did).

---

## Step 4 — Runbook: changing an environment's plan size

**File:** `docs/operations/how-to.md`

Add a self-contained section (house style: short, copy-pasteable, reuses the existing env-name table at the top of the page — do **not** restate resource-group names, reference that table). Content:

1. **Check the tier is available in the environment's region first** — this is the step that saves an aborted apply:
   ```bash
   az group show -g <rg from the table above> --query location -o tsv
   az appservice list-locations --sku P0V3 --linux-workers-enabled
   ```
   (`az` wants the SKU upper-cased here; Terraform wants `P0v3`. Note the discrepancy — it is exactly the kind of thing that costs twenty minutes.) The issue records that P0v4 was unavailable in the current zone; the same check is how you find that out for the next tier.
2. **Make the change:** edit `var.app_service_plan_sku_by_env` in `infra/variables.tf`, open a PR (CI plans `dev` automatically), then dispatch `Terraform` with `command: plan` for the target environment to see that environment's real diff, then `command: apply`. Per `release-flow.md` § *Infrastructure changes*, applies fan out per environment and are never automatic.
3. **Expect a restart.** Scaling the plan moves the app to new workers: the container re-runs `python -m qfa.cli.migrate` and reloads the embedding model, so there is a cold-start gap. `health_check_eviction_time_in_min = 10` and the severity-1 health-check alert mean a prolonged failure will page Teams — apply in a quiet window and watch `/v1/health`.
4. **Verify against Azure, not state:**
   ```bash
   az appservice plan show -n qfa-prd-plan -g qualitative-feedback-analysis-production \
     --query "{sku:sku.name, capacity:sku.capacity, tier:sku.tier}"
   ```
   Plan names are `qfa-<env>-plan` (`infra/locals.tf:5`).
5. **If the apply fails** with a scale-unit / SKU-not-supported error (a real Azure constraint — an existing plan can only be scaled to a tier its scale unit supports): the remedy is a **new** plan in a Pv3-capable scale unit, i.e. a new `azurerm_service_plan` resource plus repointing `azurerm_linux_web_app.backend.service_plan_id`, which is a longer outage and a separate PR. Record it as the known fallback; do not build it now.

**Check:** `make docs` clean. Every `az` command in the section is copy-pasteable with only `-n`/`-g` substituted from the page's existing table.

---

## Step 5 — Observability doc: what the new CPU floor means

**File:** `docs/operations/observability.md`

In § *Alerting*, after the four-row alert table (~line 279), add two sentences: thresholds are identical across environments, but prd runs 1 vCPU (P0v3) against dev/staging's 2 (B2), so the startup CPU spike from loading the embedding model is closer to the 80% line on prd; sustained firing of `high_cpu` or `high_memory` on prd is the trigger to move prd to P1v3 (ADR-019), not to raise the threshold. **Leave the table itself unchanged** — no threshold, metric, or severity moves in this PR.

**Check:** `make docs` clean; `git diff docs/operations/observability.md` touches nothing inside the table rows.

---

## Step 6 — Verify and ship

Run and report, in order:

1. `make docs` — must build clean under `-W --keep-going`. This is the mandatory gate; the new ADR must be reachable from the `docs/adr/index.md` toctree and every ADR-019 link must resolve.
2. `git status --porcelain` — only these files: `infra/variables.tf`, `infra/locals.tf`, `infra/app_service.tf`, `infra/observability.tf`, `docs/adr/019-per-environment-app-service-plan-sizing.md`, `docs/adr/index.md`, `docs/operations/deployment.md`, `docs/operations/how-to.md`, `docs/operations/observability.md`. Nothing under `src/`, `tests/`, `.github/`, or `spec/`. Nothing under `tasks/` (scratch only; leave it out of the commit).
3. `make test` and `make lint` — no Python or `pyproject.toml` change, so both are unaffected; run them once anyway to confirm the tree is clean, and say so in the PR rather than claiming they exercised the change. They do not.
4. Re-run the step 1 greps as the final infra assertion.

**Commit / PR.** One PR, one conventional commit, no trailers of any kind: `feat(infra): run the prd App Service plan on P0v3`, body `closes #204`. No version edit, no changelog file.

The PR body must carry the four things a reviewer cannot see from the diff:

- **The automatic plan on this PR shows no service-plan diff — that is correct.** It runs against `dev`, which stays B2. The prd diff requires a manual `workflow_dispatch` plan.
- The expected prd diff: one **in-place** (`~`) update, `sku_name: "B2" -> "P0v3"`. **Stop and do not apply if it renders as a replacement.**
- Apply order and blast radius: `dev` (no-op) → `staging` (no-op) → `prd` (restart). Point at the step 4 runbook.
- The decision itself, quoted from ADR-019 in two lines (prd P0v3 now, P1v3 if the alerts keep firing), so whoever closes #204 can tick "Decide if we want to upgrade to a higher instance or that we can use the instance more efficiently" against a written record. That checklist item is discharged by ADR-019 plus the merged change — there is no separate deliverable for it, and `spec/issue-204.md` is not edited.
