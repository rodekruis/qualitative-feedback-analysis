# ADR-016: Guard auto-deploy on release publish to the latest version only

## Status

Accepted

## Context

The release pipeline deploys the same container image by digest through dev →
staging → prd (promote-by-digest, see [ADR-010](010-shared-container-registry.md)).
Two of those transitions are *automatic*, triggered by the GitHub
`release: published` event:

- `auto-staging-on-publish.yaml` deploys the release's digest to **staging**.
- `docs.yaml` rebuilds the Sphinx site from the release's commit and publishes it
  to **GitHub Pages**.

The problem is that `release: published` does double duty. In GitHub it is the
only way to **un-draft** a release — to make it visible / finalize it in the
release history. This repo has *also* wired it as the **deploy trigger**. Those
two intentions are welded to one button.

The consequence: you cannot finalize an older draft release for visibility
without triggering both auto-deploys. Publishing an old draft deploys an old
image to staging *and* regresses the public docs site to an old commit. This was
the concrete pain that motivated the change — publishing an older draft
"accidentally or unknowingly" shipping an old version.

At the same time, the team explicitly wants to **keep** the ability to deploy
*older* versions on purpose — that is how rollback works — and, being a small
team (2–3 engineers), does not want a required-reviewer gate that cannot be
serviced quickly.

## Decision

Introduce a reusable **latest-release guard** (`_is-latest-release.yaml`) and gate
**both** `release: published` auto-deploy paths on it. Auto-deploy runs only when
the just-published release is the **greatest semver among all releases, drafts
included**. Manual promotion stays **version-unguarded** so rollback to older
versions still works.

Specifics:

- "Latest" is computed with a semver-aware comparison
  (`packaging.version`, not `sort -V`), so a pre-release ranks below its final
  release and pre-releases still auto-deploy to staging while they are the top of
  the version tree.
- The comparison logic lives in a unit-tested script
  (`.github/scripts/is_latest_release.py`), not inline in YAML, because this guard
  is now the only *automated* safety mechanism in the pipeline and inline workflow
  logic is untestable.
- The guard needs `contents: write` — draft releases are invisible to a
  read-only token, so without it a newer draft would be missed and the guard
  would wrongly deploy.
- The guard reads the script from the **default branch**, because a release event
  checks out the tag's commit by default and an older draft's commit predates the
  guard.
- **Fail closed**: if the latest release cannot be determined, the guard job
  fails red and nothing deploys.
- An intentional skip (publishing a non-latest release) keeps the run **green**
  and emits a loud `::warning::` + job summary explaining that nothing deployed
  and how to deploy the release deliberately.
- **No required reviewers** on any environment. Prod stays a deliberate manual
  promotion, guarded only by the existing not-draft / not-prerelease checks.

## Options Considered

### Option A: Fully decouple publish from deploy — all deploys manual (rejected)

Publishing would only change GitHub visibility; every deploy, including staging,
becomes an explicit manual action.

- **Pro**: Kills the root cause outright — publishing has zero deploy side effects.
- **Pro**: Conceptually simple; no version logic anywhere.
- **Con**: Loses the ergonomics the team values — "publish the release I just
  validated and it goes to staging" becomes an extra manual step every time.
- **Con**: Over-corrects. The pain is only with *non-latest* publishes; the
  common case (publish the newest release) was never a problem.

### Option B: Guard the automatic path, trust the manual path (chosen)

Automatic auto-deploy is guarded to latest-only (fail safe); manual promotion is
version-unguarded (fail open — the operator has expressed intent).

- **Pro**: Fixes the exact defect — publishing a non-latest draft is now safe and
  is the correct, expected way to finalize an old draft.
- **Pro**: Preserves the frictionless happy path (publish latest → staging + docs).
- **Pro**: Preserves rollback — deploying an *older* version is a distinct,
  deliberate manual act, which is exactly what the guard makes it.
- **Pro**: Small footprint — one reusable workflow + a tested script + gating
  `if:`s on two existing workflows. The safety comes from *removing* an implicit
  coupling, not from adding machinery.
- **Con**: Two behaviours to understand (auto = guarded, manual = trusted). Named
  explicitly here and in the release-flow docs.
- **Con**: The automatic staging deploy is no longer guaranteed on publish; an
  out-of-order publish silently won't move staging. Mitigated by a loud
  warning + job summary on every skip.

### Option C: Monotonic guard on every path, including prod (rejected)

Apply the "never deploy an older version" check everywhere, prod included.

- **Pro**: Uniform rule; no auto-vs-manual distinction to explain.
- **Con**: **Breaks rollback.** Rolling back is deploying an older version on
  purpose; a monotonic guard on prod would forbid the one operation you most need
  in an incident.
- **Con**: The safety it adds to manual promotion is better served by operator
  intent (and, if the team grows, required reviewers) than by a version check that
  fights the rollback use case.

## Consequences

- New reusable workflow `_is-latest-release.yaml` (input `release_tag`; outputs
  `is_greatest`, `latest_tag`; `contents: write`; fails closed).
- New unit-tested script `.github/scripts/is_latest_release.py`, covered by
  `tests/scripts/test_is_latest_release.py`.
- `auto-staging-on-publish.yaml` and `docs.yaml` gain a `guard` job; their deploy
  jobs run only when the guard confirms the release is latest. The manual
  `docs.yaml` dispatch stays ungated.
- Manual `promote-to-staging.yaml` and `promote-to-prd.yaml` are unchanged and
  remain version-unguarded (rollback path).
- The published docs site continues to reflect the latest release, not `main`.
- Prod has no codified required-reviewer gate (Terraform does not configure one);
  the release-flow docs are corrected to stop implying otherwise.

## When to revisit

- If the team grows enough to staff **required reviewers**, add them to the `prd`
  environment (in Terraform) as an additional prod gate — the guard model here is
  orthogonal to that.
- If the warning-only skip notice proves too quiet, escalate it (a comment on the
  GitHub Release, or a Slack notification).
- If release tagging ever stops being strictly semver (e.g. date-based tags), the
  definition of "latest" in `is_latest_release.py` must be revisited — it
  currently assumes semver-parseable tags and fails closed on anything else.

## Participants

Marius
