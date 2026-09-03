# ADR-021: Split the CI/CD identity: infra apply vs. image deploy

## Status

Accepted

## Context

Each environment had one managed identity (`qfa-<env>-github`), used by every
Azure-touching workflow: `terraform.yaml` (`terraform apply` — arbitrary
create/update/delete in the environment's resource group), and
`release.yaml` / `build-from-commit.yaml` / `_deploy-release.yaml` (push to
ACR, then `az webapp config container set` / `az webapp update --set
tags.*` to repoint the App Service).

That identity held `Contributor` on the resource group and `Storage Blob
Data Contributor` on the Terraform state storage account — both needed for
the Terraform path. The deploy path only ever needs to touch one App
Service resource, but ran with the same credentials: a compromised release
or promote step could delete the database, delete the Key Vault, or
rewrite Terraform state. `infra/cicd.tf`'s original comment on the
`Contributor` grant already named this as a "when to revisit" case. See
issue #80 (raised from #64's security scan) and the Copilot-flagged review
it came from.

## Decision

Split into two identities per environment:

| identity | scope | roles | used by |
|---|---|---|---|
| `qfa-<env>-github` (infra) | env RG | `Contributor` | `terraform.yaml` only |
| | tfstate SA | `Storage Blob Data Contributor`, `Reader` | |
| | shared ACR | `Reader` | |
| `qfa-<env>-github-deploy` | the env's App Service | `Website Contributor` | `_deploy-release.yaml`, `build-from-commit.yaml`, `release.yaml` |
| | shared ACR (**dev only**) | `Container Registry Repository Writer`, `Reader` | |

`Contributor` stays on the infra identity: `terraform apply` creates,
updates, and deletes App Service, Key Vault, VNet, subnets, Postgres, DNS
zones, Log Analytics, App Insights, alerts, and managed identities in that
RG — no built-in role covers that set at less than RG-Contributor breadth.
ACR push is scoped to the `dev` workspace only, because `release.yaml`'s
build job and `build-from-commit.yaml` both hardcode `environment: dev` —
no other environment's deploy identity ever writes to the shared registry.

Both identities authenticate via a federated identity credential with the
same subject (`repo:<repo>:environment:<env>`) — FIC issuer+subject
uniqueness is per-identity, so this is allowed.

## Options Considered

### Option A: Status quo — one identity (rejected)

The existing `Contributor`-on-RG reasoning is real for the Terraform path,
but it does not justify handing that same breadth to the deploy path,
which never needs it.

### Option B: Custom role definition instead of `Contributor` (rejected)

A custom `azurerm_role_definition` enumerating only the providers Terraform
touches would be narrower than `Contributor`. Rejected:
`Microsoft.Authorization/roleDefinitions/write` is not itself included in
`Contributor`, so CI could never manage its own role definition — every new
Terraform-managed resource type would require an operator-only apply to
extend the role first, and a missing action would surface as a mid-apply
403 in prd. A maintenance trap for a three-environment deployment.

### Option C: Separate GitHub environment per identity (rejected)

True subject isolation (so a compromised deploy workflow file could not
mint a token for the infra identity) requires a distinct GitHub
`environment:` per identity. Rejected: this would force PR `terraform plan`
runs through reviewer approval (environments with protection rules gate
every job that references them) and break the `terraform` required check
on PRs.

### Option D: One identity, narrower role (impossible)

`terraform apply` needs RG-Contributor breadth; no single role narrower
than `Contributor` covers both the Terraform and deploy use cases.

## Consequences

- Compromise of the deploy identity (or a step using it) can no longer
  delete Postgres, the Key Vault, the VNet, or tamper with Terraform state,
  and can no longer poison the shared ACR from a non-dev environment.
- Two residual risks remain, deliberately not oversold:
  - **Same-environment token minting.** Both identities share a federated
    credential subject, so any job running in the `<env>` GitHub
    environment with `id-token: write` can still request a token for
    either identity — the split is defence-in-depth against a compromised
    deploy *step*, not isolation against a fully compromised workflow file
    (see Option C).
  - **Secret exfiltration via a malicious image.** `Website Contributor`
    can repoint the App Service's container image, and the App Service's
    system-assigned managed identity holds `Key Vault Secrets User`. A
    malicious image deployed through the deploy identity can still read
    Key Vault secrets at runtime. What this ADR removes is destruction and
    state tampering, not this path.
- Role-assignment changes in `infra/cicd.tf` remain operator-apply-only
  (already true before this ADR for the existing grants) — CI's infra
  identity has `Reader`, not `Microsoft.Authorization/roleAssignments/write`,
  on the scopes involved. See [Roll out the split CI/CD
  identities](../operations/how-to.md#roll-out-the-split-cicd-identities-to-an-existing-environment)
  for the one-time migration this required on already-provisioned
  environments.

## Participants

Olaf
