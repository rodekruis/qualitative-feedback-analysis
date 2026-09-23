# ADR-023: Derive the deploy digest from the registry, cross-checked against the release body

## Status

Accepted

## Context

GitHub Actions workflows drive deploys. `release.yaml` writes an image
digest into a GitHub Release body. The promote and deploy workflows read
that digest back to decide what to run. See
[ADR-010](010-shared-container-registry.md).

`_deploy-release.yaml` picked the image to deploy by finding the first
`sha256:` digest in the release body, and the release body is editable
Markdown. Anyone with `write` or `maintain` access was able to edit an
already-published release. Editing let them prepend a forged digest that
pointed at any image in Azure Container Registry (ACR).

The verify jobs in `promote-to-prd.yaml` and `promote-to-staging.yaml`
looked only at the draft and pre-release flags. A body edit did not change
those flags, so a forged digest deployed anyway. `auto-staging-on-publish.yaml`
had no verify job at all. Nothing cryptographic tied a tag to a digest.
See #173.

The gap was fully live at the time of this decision. The `dev`, `staging`,
and `prd` GitHub environments in this repository all had
`protection_rules: []`, so no required reviewers gated any of them. All 10
collaborators held `admin`. Every collaborator was able to edit a published
release body and change what deployed to production, with no human gate in
between. This measurement matches the choice recorded in
[ADR-016](016-guard-auto-deploy-on-publish.md) and
`docs/operations/release-flow.md` to run without a required-reviewer gate
for a small team. This ADR records that measurement and does not revisit
the choice.

This service handles sensitive beneficiary data. An unvetted image can
reach staging or prd without a new release. That breaks a guarantee the
release pipeline exists to provide: an image in prd came from a reviewed
release.

## Decision

`_deploy-release.yaml` resolves `qfa-backend:<tag>` to a digest from ACR,
and requires the release body to name the same digest
(`.github/scripts/verify_release_digest.py`). Both values must agree
before a deploy proceeds. `auto-staging-on-publish.yaml` also gains a
`verify` job that re-reads the release from the API and rejects a draft.
This job makes the gate set uniform across all three staging and prd entry
points.

Specifics:

- The registry decides which bytes a tag names, not the release body. The
  body becomes a mandatory, independently-editable witness that must agree
  with the registry. It is no longer the source of the deployed value.
- The comparison lives in a unit-tested script
  (`.github/scripts/verify_release_digest.py`), not inline in YAML. This
  follows the same reasoning as the latest-release guard in ADR-016.
  Inline workflow logic only runs in anger, on the exact event this change
  tries to make safe.
- A body with no digest still aborts the deploy. This matches the old
  behavior.
- A body with the digest repeated is accepted. The rule counts distinct
  digests, not matches.
- A body with more than one distinct digest aborts the deploy, no matter
  how incidental the extra value is. For example, a changelog line can
  happen to contain a `sha256:` value. This fails closed by design. A
  stricter alternative parses only a fixed `**Digest**:` line, but that
  lets an attacker hide a forged digest outside that line. That defeats
  the purpose.
- The verification script reads the release body from stdin. The body
  never appears as an argument value or a shell interpolation, because the
  body is attacker-controlled Markdown.
- `_deploy-release.yaml` keeps `contents: write`. The dev auto-deploy path
  still reads a still-draft release, so this permission stays required.

## Options Considered

### Option A: Body only, the status quo (rejected)

Keep resolving the digest from the release body, unchanged.

- Con: This is the vulnerability. Nothing changes.

### Option B: ACR only (rejected)

Resolve the digest from ACR and stop reading the release body.

- Pro: Removes the editable-Markdown attack surface entirely.
- Con: Moves the trust anchor to a mutable ACR tag instead. The ACR runs
  on Basic SKU (`infra/bootstrap.sh`), has no tag lock configured, and the
  CI identity holds push access to it. An attacker able to retag
  `qfa-backend:<tag>` in ACR faces the same gap that exists today. The
  vulnerability class stays, only its location moves.

### Option C: ACR plus mandatory body agreement (chosen)

Resolve the digest from ACR, then require the release body to name the
same digest.

- Pro: Stronger than either A or B alone. A forged deploy now needs both
  GitHub release-edit access and ACR push access, not either one alone.
- Pro: A lone body edit fails, which fixes the gap in Option A. A lone ACR
  retag also fails, which closes the gap in Option B.
- Con: Two mutable surfaces must agree with each other. That is agreement,
  not a cryptographic tag-to-digest binding. See Residual risk below.

## Consequences

- `_deploy-release.yaml` resolves `qfa-backend:<tag>` to a digest from
  ACR, then verifies the release body agrees, before it deploys. See the
  rewritten step sequence and header comment in that file.
- A new unit-tested script, `.github/scripts/verify_release_digest.py`,
  backs this verification. `tests/scripts/test_verify_release_digest.py`
  covers it.
- A new regression test, `tests/scripts/test_deploy_release_integrity.py`,
  pins the step order: login before ACR resolution, verification before
  deploy, and no body grep. The same test also verifies, across every
  workflow file, that no `run:` step interpolates `inputs.*`,
  `github.event.*`, or `github.head_ref` directly. That is the same
  vulnerability class as #173, applied to shell scripting generally.
- `auto-staging-on-publish.yaml` gains a `verify` job. Both `terraform`
  and `deploy` depend on it.
- `build-from-commit.yaml` and `terraform.yaml` now bind their own
  event-influenced `run:` interpolations, `ref` and `environment`, to
  `env:` instead of splicing them into shell. This closes the same class
  of gap found while auditing for this change.
- `release.yaml` must keep writing the digest into the release body. The
  digest now matters for verification, not only for documentation.
- `build-from-commit.yaml` images still cannot reach staging or prd. They
  push an `ephemeral-*` ACR tag and have no GitHub release, so the
  promotion workflows have nothing to resolve or verify.

Residual risk: this decision creates agreement between two mutable
surfaces, not a cryptographic tag-to-digest binding. If the threat model
tightens, two upgrades exist:

- Per-tag ACR lock: `az acr repository update --image qfa-backend:<tag>
  --write-enabled false` works on Basic SKU. This change deliberately
  skips that lock. A write-disabled tag rejects later pushes to the same
  tag, so re-running a release for that tag fails at push.
- Build provenance or image signing, for a binding that does not depend on
  either surface staying honest.

Fail-closed also means a changelog line that legitimately contains a
second `sha256:` value blocks the deploy. This is by design. See the
Decision section.

## When to revisit

- If a security incident or an audit requires a cryptographic
  tag-to-digest binding instead of two-surface agreement, adopt per-tag
  ACR locking or image signing. See Residual risk above.
- If the team grows enough to staff required reviewers, add them to the
  `staging` and `prd` environments as an additional gate. This step is
  independent of this decision, the same note as in ADR-016.
- If the fail-closed behavior on an incidental second `sha256:` match in
  release notes proves too disruptive in practice, narrow the parser to a
  fixed section of the body. The Decision section above explains why that
  narrowing is a deliberate trade-off, not an oversight.

## Participants

Olaf
