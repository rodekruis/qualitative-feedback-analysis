# ADR-009: Dedicated Resource Group for Terraform State Storage

## Status

Accepted

## Context

Terraform requires a remote state backend (an Azure Storage Account) to
exist before `terraform init` can run — a chicken-and-egg dependency that
lives outside Terraform's own management. The question is where that
storage account should live: in each environment's resource group, in
a shared platform resource group, or in its own dedicated resource group.

The project uses Terraform workspaces to manage three environments
(dev, staging, prd), each with its own resource group. Terraform state
is per-workspace but the storage account and blob container are shared
across all workspaces — each workspace writes to a different state key
within the same container.

## Decision

Place the Terraform state storage account in a conceptually separate
resource group, distinct from both the per-environment resource groups
and the shared platform resource group (which hosts the ACR).

In `bootstrap.sh` and `BOOTSTRAP.md`, this is expressed via the
`TF_VAR_tf_state_resource_group_name` environment variable. For a
minimal single-RG deployment, operators may point it at the same RG as
everything else; for a multi-RG deployment, it gets its own dedicated RG.

## Options Considered

### Option A: State in each environment's RG (rejected)

Each environment (dev, staging, prd) would have its own storage account
holding its own state file.

- **Pro**: Self-contained — `terraform destroy` on an environment cleans
  up everything including its state.
- **Con**: Circular dependency — the storage account must exist before
  `terraform init`, but `terraform destroy` would delete it, leaving
  Terraform unable to reason about what it just destroyed. Recovery
  requires recreating the storage account and re-importing state.
- **Con**: Multiplies bootstrap work — every new environment needs its own
  pre-Terraform `az storage account create` step, increasing the
  operational burden and the number of globally-unique names to manage.
- **Con**: No shared state history — you lose the ability to inspect or
  compare state across environments from a single location.

### Option B: State in a shared platform RG alongside the ACR (rejected)

One shared resource group holds both the Terraform state storage account
and the container registry.

- **Pro**: Fewer resource groups to manage. One bootstrap step creates
  both the state backend and the ACR.
- **Con**: Conflates two resources with different lifecycle and access
  properties. The ACR is a shared, pull-heavy, promotable artifact store
  that may eventually be geo-replicated or granted cross-team read access.
  The state storage is a write-heavy, environment-internal, never-shared
  data store. Mixing them means ACR lifecycle operations (retention
  policies, geo-replication, third-party access grants) risk accidentally
  affecting state storage, and vice versa.
- **Con**: Different ownership boundaries — the ACR may eventually be
  delegated to a platform team, while state storage should remain in the
  hands of whoever owns Terraform. Co-locating them in one RG makes this
  delegation harder.

### Option C: State in its own dedicated RG (chosen)

A dedicated resource group (e.g. `rg-qfa-tfstate`) holds only the
Terraform state storage account, a blob container, and a `CannotDelete`
lock.

- **Pro**: Clean blast radius — accidental deletion of an environment RG
  or the platform RG cannot destroy state.
- **Pro**: Minimal surface — the RG contains exactly one resource, making
  RBAC and lifecycle management trivial. Only principals that run
  `terraform init` / `apply` need access.
- **Pro**: Adding a new environment requires zero changes to the state
  infrastructure — `terraform workspace new <env>` writes a new state key
  in the existing container.
- **Pro**: The `CannotDelete` lock on the storage account provides defense
  in depth. An accidental `az storage account delete` is the single most
  destructive operation in this architecture; isolating the SA in its own
  locked RG makes that accident harder to trigger.
- **Con**: One more resource group to create during bootstrap (a one-time
  `az group create` command).

## Consequences

- `bootstrap.sh` reads `TF_VAR_tf_state_resource_group_name` to know
  where to create the storage account. This variable is shell-only — it
  cannot be a Terraform variable because backend blocks forbid variable
  interpolation.
- `terraform init` receives the same value via
  `-backend-config="resource_group_name=..."`.
- `bootstrap.sh` adds a `CannotDelete` lock to the storage account
  immediately after creation.
- For a single-RG deployment, `TF_VAR_tf_state_resource_group_name` can
  equal `TF_VAR_resource_group_name` — the conceptual separation exists in
  the variable structure even when the physical RGs are the same.
- Migrating from single-RG to multi-RG is a config change (re-export env
  vars, re-init with `-backend-config`, migrate state), not a code change.

## Participants

- Architect (proposed dedicated RG — clean lifecycle separation)
- Devil's advocate (challenged the added RG — one-time creation cost is
  negligible, accepted)
