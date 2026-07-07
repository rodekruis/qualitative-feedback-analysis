# Spec: Guard auto-deploy on release publish

## Objective

Decouple "publishing a GitHub release" (making it visible/final) from "deploying
it," so that publishing an **old** draft release to finalize it no longer has
destructive side effects.

**Root cause.** The `release: published` event does double duty — it is both
GitHub's "un-draft / make visible" action *and* this repo's deploy trigger — and
it is wired to **two** automatic paths:

- `auto-staging-on-publish.yaml` → deploys the release's digest to **staging**.
- `docs.yaml` → rebuilds the Sphinx site from the release's commit and publishes
  it to **GitHub Pages**.

So publishing a non-latest draft today deploys an old image to staging *and*
regresses the public docs site to an old commit. There is no way to finalize an
old draft for visibility without triggering both.

**Users.** The 2–3 engineers who cut and promote releases via GitHub Actions.

**Success.** You can publish any old draft for visibility with zero deploy side
effects, while the normal flow (cut → validate in dev → publish → staging →
promote to prd) is unchanged, and rollback to older versions still works via
manual promotion.

## Tech Stack

- GitHub Actions (reusable workflows); Azure App Service + ACR
  (promote-by-digest, see ADR-010).
- Python 3.12 + `packaging` (present in the environment) for **semver-aware**
  version comparison, run via `uv run --no-project --with packaging`. No new
  declared dependency.
- Sphinx docs (`make docs`); ADRs under `docs/adr/`.

## Commands

```
Lint:            make lint
Test:            make test
Docs build:      make docs
Guard unit test: uv run pytest tests/scripts/test_is_latest_release.py
Guard locally:   uv run --no-project --with packaging python \
                   .github/scripts/is_latest_release.py \
                   --tag v0.3.0 --all-tags v0.3.0,v0.6.0,v0.6.0-rc.1
                 # exits 0 and prints is_greatest=false / latest=v0.6.0
```

## Project Structure

```
.github/scripts/is_latest_release.py              → testable semver "is this the greatest release?" logic
.github/workflows/_is-latest-release.yaml         → reusable guard workflow (thin: calls the script, exposes outputs)
.github/workflows/auto-staging-on-publish.yaml    → staging deploy, gated on the guard
.github/workflows/docs.yaml                        → release-triggered Pages deploy, gated on the guard
tests/scripts/test_is_latest_release.py           → pytest cases for every scenario below (repo convention: tests/scripts/)
docs/adr/016-guard-auto-deploy-on-publish.md      → decision record
docs/adr/index.md                                  → +016 table row & toctree entry
docs/operations/release-flow.md                    → updated behavior + recovery paths; corrected reviewer claims
```

## Code Style

The reusable workflow stays **dumb**; the brains live in a **unit-tested** script.
Everything a workflow does inline is effectively untested — it only runs in anger,
on the exact event we are trying to make safe. So the decision logic is extracted
into `.github/scripts/is_latest_release.py`, a pure function `is_latest(tag,
all_tags)` plus a thin CLI. The script is imported in tests via `importlib` from its
file path (the repo's existing `tests/scripts/` convention), because `.github/scripts/`
is not a Python package on `sys.path`.

The workflow reads `contents: write` (required to list *drafts*), gathers all release
tags with `gh release list`, and gates the deploy job on the script's `is_greatest`
output. Exit codes: `0` for a definite true/false answer, `2` (fail closed) when the
guard cannot determine the latest release.

Conventions: conventional-commit subjects only (no trailers); every test function
has a one-line docstring stating what + why; docs cite module/workflow names, not
`file:line`.

## Testing Strategy

- **Framework:** pytest, run under `uv`. Tests live in `tests/scripts/` (mirrors the
  existing `tests/scripts/test_stress_analyze.py`).
- **What is tested:** the extracted `is_latest_release.py` — the only place the
  safety logic lives. There is no harness in this repo that executes whole GitHub
  workflows, so workflow YAML is validated by a `yaml.safe_load` parse + a manual dry run.
- **Coverage:** every decision branch of `is_latest`, plus the CLI exit codes.
  Required scenarios:

  | # | Scenario | Expected |
  |---|----------|----------|
  | 1 | tag is strictly greatest among all (incl. drafts) | `true` |
  | 2 | a newer **published** release exists | `false` |
  | 3 | a newer **draft** exists (load-bearing case; workflow feeds drafts into the tag list) | `false` |
  | 4a | publish `v1.0.0-rc.1` while final `v1.0.0` exists | `false` |
  | 4b | publish `v1.0.0` while only `v1.0.0-rc.1` exists | `true` |
  | 5 | rc ordering: `v0.6.0-rc.2` vs `v0.6.0-rc.1` | rc.2 → `true` |
  | 6 | tag is the only release | `true` |
  | 7a | stray non-semver tag alongside real releases | ignored; real greatest wins |
  | 7b | the **published** tag itself is unparseable | raises → CLI exit 2 (fail closed) |
  | 7c | no parseable tag in the list at all | raises → CLI exit 2 (fail closed) |
  | 8 | tag absent from list | raises → CLI exit 2 (fail closed) |

- **Manual verification before merge:** on a scratch repo/branch, publish a draft
  that is *not* the latest and confirm: neither staging nor Pages deploys, the run
  is green, and the warning + step-summary message appears.

## Boundaries

- **Always:**
  - Run `make lint`, `make test`, and the guard pytest before committing.
  - Keep docs in sync in the same change: ADR-016 + `docs/adr/index.md` +
    `docs/operations/release-flow.md`.
  - **Fail closed** — if the guard cannot determine the latest release, do not
    deploy and fail the run red.
  - On an intentional skip (tag not latest), keep the run **green** and emit the
    dummy-proof warning to a `::warning::` annotation *and* `$GITHUB_STEP_SUMMARY`.
  - Give the guard `contents: write` so it can see drafts.

- **Ask first:**
  - Changing manual-promotion behavior (staging/prd promotes stay version-unguarded).
  - Adding required reviewers or any new gate to prod.
  - Adding a new *declared* dependency, or changing the definition of "latest."

- **Never:**
  - Push to `main`, force-push, or merge.
  - Make the auto-deploy paths fail **open** (deploy on guard error/uncertainty).
  - Remove the not-draft / not-prerelease checks on `promote-to-prd`.
  - Guard the **manual** docs dispatch or manual promotes (those are trusted).
  - Post the skip notice anywhere beyond the run (warning + summary only — decided).
  - Commit secrets.

## Success Criteria

1. Publishing a **non-latest** draft → staging deploy **and** docs Pages deploy are
   both skipped; the run is green; the warning + step summary show the recovery message.
2. Publishing the **genuine latest** release → staging and docs deploy exactly as today.
3. Pre-releases still auto-deploy to staging **when they are the top of the version
   tree**, ranked correctly (`rc` < final) by the semver comparator (not `sort -V`).
4. `promote-to-staging` / `promote-to-prd` still deploy **any** specified tag
   regardless of version (rollback preserved).
5. Genuinely undecidable state (published tag unparseable, tag absent, or no
   parseable release at all) → run fails **red**, nothing deployed. A stray
   non-semver tag *alongside* real releases is ignored (logged), not fatal.
6. `is_latest_release.py` has passing pytest covering all scenarios in the table.
7. ADR-016 added and indexed; `release-flow.md` updated; inaccurate reviewer-approval
   claims corrected (reviewers are not codified in Terraform).
8. `make lint`, `make test`, and `make docs` all pass.

## The dummy-proof skip message

```
⚠️ Release <TAG> is now published and visible — but NOTHING was deployed.
Staging and the docs site were NOT updated, because a newer release (<LATEST>)
already exists. Auto-deploy on publish only runs for the latest version.
This is expected when you publish an older draft to finalize it.

If you really did mean to deploy <TAG>:
  • Staging → run "Promote to staging" with tag <TAG>
  • Docs    → run the "Docs" workflow manually (workflow_dispatch)
  • Prod    → run "Promote to prd" with tag <TAG>
```

## Resolved decisions (formerly Open Questions)

1. **Spec location** — moved to `docs/specs/` (this file). ✅
2. **Skip-notice reach** — warning annotation + step summary only; no release comment. ✅
3. **Cleanup scope** — stale `build-release-image.yaml` references in
   `_deploy-release.yaml` and `promote-to-dev.yaml` are fixed in this PR. ✅
