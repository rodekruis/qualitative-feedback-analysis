# ADR-010: Shared Container Registry Across Environments

## Status

Accepted

## Context

The project deploys the same application to three environments (dev,
staging, prd). Each environment runs a container image pulled from an
Azure Container Registry (ACR). The question is whether to use one
shared ACR for all environments or a separate ACR per environment.

A core design goal of the deployment pipeline is **promote-by-digest**:
build a container image once, push it once, and deploy the exact same
bytes (identified by their immutable SHA-256 digest) through dev →
staging → prd. This guarantees bit-identical artifacts across
environments, eliminating "works in dev but not in prod" failures caused
by non-deterministic builds.

## Decision

Use a single shared ACR, hosted in a dedicated platform/shared resource
group (via `TF_VAR_acr_resource_group_name`), referenced by all
environments as a Terraform `data` source.

## Options Considered

### Option A: One ACR per environment (rejected)

Each environment (dev, staging, prd) gets its own ACR in its own
resource group.

- **Pro**: Clean blast radius — compromising dev's ACR credentials cannot
  affect prod's images. A `terraform destroy` on dev deletes only dev's
  images.
- **Pro**: No cross-RG IAM needed — each ACR lives in its own environment
  RG, so the managed identity only needs roles on local resources.
- **Con**: **Breaks promote-by-digest.** The same image built on one ACR
  must be copied to each subsequent ACR (via `az acr import` or similar).
  Even with digest-preserving copies, you now have N copies of the same
  bytes, N security scans, N retention policies, and the promotion
  pipeline needs explicit copy steps. This adds complexity and latency
  to every promotion.
- **Con**: Images with the same tag on different registries are not
  guaranteed to be bit-identical unless you copy by digest. This defeats
  the primary benefit of containerization (immutable, promotable
  artifacts).
- **Con**: Higher cost — storage is duplicated across registries.
- **Con**: The blast-radius benefit is largely theoretical at this
  project's scale: same subscription, same tenant, same team, same CI
  system. True isolation requires separate subscriptions, not just
  separate ACRs.

### Option B: One shared ACR, all environments pull from it (chosen)

A single ACR lives in a shared resource group. Dev CI pushes images.
All environments pull from the same registry by digest.

- **Pro**: **Build once, promote by digest.** The image pushed during
  `release.yaml` is the exact same bytes that run in dev, staging, and
  prd. No copies, no rebuilds, no divergence.
- **Pro**: One place for security scanning, SBOMs, retention policies,
  and geo-replication.
- **Pro**: Promotion is a pointer change (update the App Service's image
  reference to a different digest), not a data movement operation.
  Rollback is the same operation in reverse.
- **Pro**: Cheaper — one copy of each image, one storage bill.
- **Con**: The ACR becomes a cross-environment dependency. If it has an
  outage, no environment can cold-start new instances. Mitigation: ACR
  Premium supports geo-replication (a future upgrade path, not needed at
  current scale).
- **Con**: Broader IAM surface — dev's CI identity needs `AcrPush`,
  staging and prd's App Service identities need `AcrPull`, all on the
  same registry. This is manageable via resource-scoped role assignments
  (already in place in `cicd.tf` and `app_service.tf`).

### Option C: Per-environment ACRs with digest-preserving promotion copies (rejected)

Each environment has its own ACR. Promotion copies the image by digest
via `az acr import`, preserving bit-identity.

- **Pro**: Bit-identical artifacts (same as B) plus environment isolation
  (same as A).
- **Pro**: Each ACR's IAM is minimal and environment-scoped.
- **Pro**: Promotion is an explicit, auditable event.
- **Con**: More moving parts — requires a promotion pipeline (or manual
  `az acr import`) for each environment transition. Storage duplicated.
- **Con**: Overkill at this project's scale (small team, single
  subscription, three environments). The operational overhead of managing
  promotion copies outweighs the isolation benefit until the team grows
  or compliance requirements mandate it.

## Consequences

- The ACR is created by `bootstrap.sh` in
  `$TF_VAR_acr_resource_group_name` and referenced in Terraform via
  `data "azurerm_container_registry"` in `container_registry.tf`.
- `release.yaml` builds and pushes images to the shared ACR, tagged with
  the release version. The registry-assigned digest is captured and
  written into the GitHub Release body.
- Promotion workflows (`promote-to-staging.yaml`, `promote-to-prd.yaml`)
  read the digest from the release body and update the target App
  Service's image reference. No image copy or rebuild occurs.
- Rollback is symmetric with promotion: the same `az webapp config
  container set` command, pointing at a previous release's digest.
- The App Service's system-assigned managed identity gets
  `Container Registry Repository Reader` on the shared ACR
  (in `app_service.tf`). The GitHub Actions identity gets
  `Container Registry Repository Writer` (in `cicd.tf`).
- Cross-RG RBAC works identically to same-RG RBAC — Azure role
  assignments are scoped to the target resource ID, not to the
  principal's resource group.

## When to revisit

- If a second team with a different release cadence onboards and needs
  its own promotion pipeline.
- If a security incident involving dev credentials leaks makes ACR
  isolation a requirement.
- If a funder or auditor requires physical environment separation in
  writing.

In any of these cases, upgrade to Option C (per-environment ACRs with
digest-preserving copies). The promote-by-digest pipeline is already
in place; the change is adding a copy step and a per-environment
registry, not redesigning the promotion model.

## Participants

- Architect (proposed shared ACR — promote-by-digest requires it)
- Domain expert (validated that compliance does not currently require
  environment isolation at the registry level)
- Devil's advocate (challenged cross-environment blast radius — accepted
  that the risk is theoretical at current scale and manageable via RBAC)
