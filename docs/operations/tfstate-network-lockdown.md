# Restricting CI/CD Blast Radius and Locking Down Terraform State

Addresses two related security findings ([#80](https://github.com/rodekruis/qualitative-feedback-analysis/issues/80), [#176](https://github.com/rodekruis/qualitative-feedback-analysis/issues/176)):

- The single GitHub Actions identity previously held Contributor on the resource group *and* was used by every workflow, including ones that only ever touch ACR/App Service.
- The Terraform state storage account had no network restriction — reachable from any network, gated only by Azure AD data-plane auth.

## What changed

Two identities instead of one (`infra/cicd.tf`):

| Identity | Roles | Used by | Auth |
|---|---|---|---|
| `github_terraform` | Contributor on the RG, `Storage Blob Data Contributor`/`Reader` on the tfstate SA, `Reader` on the ACR | `terraform.yaml` only | VM-attached user-assigned identity (IMDS), **not** OIDC |
| `github_deploy` | `Container Registry Repository Writer`, `Website Contributor` on the App Service only | `build-from-commit.yaml`, `release.yaml`, `_deploy-release.yaml`, `promote-to-*.yaml` | GitHub-federated OIDC (unchanged) |

`terraform.yaml` now runs on a self-hosted runner living inside `qfa_vnet`, reaching the Terraform state storage account over a private endpoint. Every other workflow is unaffected — they never touch the state account, so they stay on `ubuntu-latest`.

## Why not just IP-allowlist GitHub's runners

`ubuntu-latest` jobs run on a large, rotating pool of Microsoft-managed IPs (published, but frequently changing). Allowlisting that range on the state account's firewall is a much weaker guarantee than it looks — closer to "allow most of Azure's public cloud" than "allow our CI" — and requires an ongoing job to keep the firewall in sync as the range rotates. A private endpoint removes the account from the public internet entirely; only the VNet (and explicitly allowlisted operator IPs, for local `terraform apply`) can reach it.

## Rollout order (read before touching any of this)

This is **not** a single atomic change — merging the code without doing the manual steps in order will strand CI. Follow this sequence exactly:

### 1. Apply the Terraform changes (safe, additive)

```bash
cd infra
terraform apply  # per environment/workspace, as usual
```

This creates the `github_terraform`/`github_deploy` identities, the runner subnet (`qfa-<env>-runner-snet`, `10.0.3.0/24`), and the private-endpoint subnet (`qfa-<env>-tfstate-pe-snet`, `10.0.4.0/24`). Nothing here restricts network access yet — the state account is still fully public at this point, so this step is safe to merge and apply on its own.

Set the new repo-scoped variable:

```bash
gh variable set AZ_TERRAFORM_CLIENT_ID --repo "rodekruis/qualitative-feedback-analysis" \
  --body "$(terraform output -raw az_terraform_client_id)"
```

### 2. Stand up the self-hosted runner VM

Manual, one-time, per deployment:

1. Create a small VM in `qfa-<env>-runner-snet` (no public IP — it only needs to reach the private endpoint created in step 4, plus GitHub's Actions API for job polling, which needs outbound internet — see note below).
2. Attach the `github_terraform` identity: `az vm identity assign --name <vm> --resource-group <rg> --identities <az_terraform_client_id resource ID>`.
3. Register it as a GitHub Actions runner with label `qfa-tfstate-runner` (Settings → Actions → Runners → New self-hosted runner; install as a service with `svc.sh install && svc.sh start` so it survives reboots).

> **Note on egress:** the runner still needs outbound internet to poll GitHub for jobs (GitHub Actions runners are always polling/pull-based, not inbound). Only the *Terraform state account* is being locked down here — the runner is not fully air-gapped. If a fully egress-locked runner is ever wanted, that's a separate, larger change (NAT gateway + explicit allowlist of GitHub's endpoints).

### 3. Verify before locking anything down

Trigger `terraform.yaml` via `workflow_dispatch` (`plan`, any environment) and confirm the self-hosted runner picks it up and `terraform init`/`plan` succeed via `ARM_USE_MSI`. At this point the state account is still public, so this only proves the runner + identity path works — it does not yet prove the lockdown will succeed.

### 4. Run the lockdown script

Only after step 3 passes:

```bash
export TF_VAR_tf_state_resource_group_name=<rg-where-state-lives>
export TF_VAR_tf_state_storage_account=<state-storage-account-name>
export TF_VAR_resource_group_name=<rg-where-vnet-lives>
export VNET_NAME=qfa-<env>-vnet
export PE_SUBNET_NAME=qfa-<env>-tfstate-pe-snet
export OPERATOR_IPS="<ip1> <ip2>"  # everyone who runs terraform apply/plan locally

bash infra/lockdown-tfstate-network.sh
```

This creates the private endpoint + private DNS zone, then sets `--default-action Deny` on the state account with an IP allowlist for `OPERATOR_IPS`.

Like `bootstrap.sh`, this script manages a resource that lives outside Terraform (the state account itself) — kept separate from `bootstrap.sh` because it is not safe to blindly re-run (re-running the IP-rule step after operator IPs have changed needs deliberate review).

### 5. Re-verify after lockdown

Trigger `terraform.yaml` again on the self-hosted runner — it should still succeed. If you have any lingering `ubuntu-latest` job that touches the state account, confirm it now fails (expected — that's the point).

## Ongoing operational notes

- **New operator**: add their IP with `az storage account network-rule add --account-name <sa> --resource-group <rg> --ip-address <ip>`.
- **Runner VM rebuilt**: repeat step 2 above (identity attach + runner registration); no Terraform changes needed.
- **Runner OS patching**: unlike `ubuntu-latest`, this VM does not get a fresh image per run — patch it like any other long-lived VM.
- **If the `github_terraform` or `github_deploy` identity is ever recreated** (e.g. after `terraform destroy`): re-run [Set up a new environment § 5](setup-new-env.md#5-configure-the-environments-github-variables) for the affected environment, and re-attach the (new) `github_terraform` identity to the runner VM.
