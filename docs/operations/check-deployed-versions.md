# Check deployed versions

How to find out which **application version** and which **infrastructure commit**
are live in `dev`, `staging`, and `prd` — including when the app itself is down
and the health endpoint is unreachable.

For *how* versions get there in the first place, see [Release flow](release-flow.md).
For the runtime picture, see [Deployment: runtime overview](deployment.md).

## Quick answer: the helper script

[`scripts/show_deployed_versions.sh`](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/scripts/show_deployed_versions.sh)
prints the deployed app version and infra commit for every environment using
only the GitHub paper trail (needs `gh` and `jq`, no Azure access):

```console
$ scripts/show_deployed_versions.sh
== dev ==
  app:   v2.0.0 @ sha256:8a994c…  [Release]  2026-06-11T15:18:38Z  ref=main  sha=23247174df94
  infra: 2026-06-11T17:22:46Z  ref=main  sha=8a994cd2e6b7  (Terraform apply)
== staging ==
  app:   v2.0.0 @ sha256:8a994c…  [Auto-deploy published release to staging]  ...
...
```

The sections below explain where those numbers come from and how to read them
by hand.

## Live app version: the health endpoint

When the app is up, every environment exposes its application version at
`GET /v1/health`:

```bash
curl https://<app-host>/v1/health   # → {"status":"ok","version":"2.0.0"}
```

The version is `qfa.__version__`, read from the installed package. It is the
ground truth for *what is actually serving* — but it only reports the **app**
version, and it is unavailable when the App Service is down.

## When the app is down: App Service tags

Every deploy stamps the version onto the App Service as **Azure resource tags**
(`deployed_version`, `deployed_digest`, `deployed_at`, `deployed_by`). Tags are
control-plane metadata, so they are readable even when the container will not
start:

```bash
az webapp show -g <resource-group> -n <app-name> --query tags
```

This is the answer to "the app is down — what was deployed here?". The container
image reference carries the same digest independently
(`az webapp config container show`), and because tags are control-plane only,
writing them on deploy does **not** restart the app. Terraform is configured to
ignore these tag keys, so an infra apply will not wipe them.

## The GitHub paper trail

Every deploy and every Terraform run declares a GitHub `environment`, so GitHub
records a **deployment** object for each one. These are queryable per
environment without touching Azure:

```bash
gh api "repos/{owner}/{repo}/deployments?environment=prd&per_page=5" \
  --jq '.[] | {id, ref, sha: .sha[0:12], created_at}'
```

The records are accurate, but three properties make the raw list misleading on
its own — read them before trusting a single field:

```{important}
- **Infra and app deploys are interleaved.** Both the app deploy (the reusable
  `_deploy-release` workflow) and the `Terraform` workflow declare an
  environment, so both produce `task: deploy` records. The deployment record
  alone does not say which kind it was — you must follow it to its originating
  workflow (see below).
- **Terraform `plan` runs also appear.** The `Terraform` workflow declares its
  environment for *every* run, including the `plan` it runs on pull requests.
  A deployment record can therefore correspond to a plan that changed nothing.
  Real applies come from `workflow_dispatch` runs; plans come from
  `pull_request` / `push`.
- **The deployment `ref` is not the version.** `Promote to prd` runs from
  `main`, so its deployment `ref` is `main`, not the released tag. The version is
  instead recorded in the deployment **status description**
  (`v2.0.0 @ sha256:…`), written by the deploy pipeline — read that, not `ref`.
  (`Promote to staging` and the publish-triggered auto-deploy happen to run
  *from* the tag, so for `staging` the `ref` is the version too.)
```

```{note}
The status description is populated by the `annotate` job in
`_deploy-release.yaml`, which runs after the deploy so its status is the latest
one — that is why the release tag, not the bare commit, shows on the repo's
Environments page. Deploys from before this was added have an empty description;
fall back to the run logs (below) for those.
```

The reliable disambiguator is the **originating workflow name**. Each deployment
has a status whose `target_url` points at the workflow run that created it:

| Originating workflow | What the record means |
|---|---|
| `Terraform` | An infrastructure plan or apply |
| `Release` | App auto-deployed to `dev` at release time |
| `Auto-deploy published release to staging` | App deployed to `staging` on publish |
| `Promote to dev` / `Promote to staging` / `Promote to prd` | App promoted to that environment |

## Application version per environment

The deployed app version is the release **tag** (and its immutable image
digest). The most direct read, for any environment, is the latest app
deployment's status description:

```bash
ENV=prd
ID=$(gh api "repos/{owner}/{repo}/deployments?environment=$ENV&per_page=1" --jq '.[0].id')
gh api "repos/{owner}/{repo}/deployments/$ID/statuses" \
  --jq 'first(.[] | select(.description != "") | .description)'
# → v2.0.0 @ sha256:…
```

(`scripts/show_deployed_versions.sh` does exactly this, for all environments,
and skips Terraform records.) Two notes per environment:

- **`dev`** — `Build from commit` publishes an `ephemeral-<branch>-<sha>` image
  that is **not** a release, so a `dev` deploy is not always a released version.
  The App Service tags and `GET /v1/health` reflect whichever image is live.
- **older deploys** — deployments cut before the status description was added
  have an empty description. Fall back to the run logs: the `verify` job echoes
  the tag and the deploy job logs the digest.

  ```bash
  RUN=$(gh run list --workflow=promote-to-prd.yaml --status success -L 1 \
    --json databaseId --jq '.[0].databaseId')
  gh run view "$RUN" --log | grep -E 'TAG:|Deploying v|sha256:'
  ```

## Infrastructure version per environment

Infrastructure has no version tag — the deployed "version" is the **git commit**
whose `infra/` Terraform state was last *applied*. Find the last apply for an
environment by filtering Terraform runs to manual dispatches:

```bash
gh run list --workflow=terraform.yaml --event workflow_dispatch --status success \
  -L 5 --json databaseId,headBranch,headSha,createdAt \
  --jq '.[] | {id:.databaseId, ref:.headBranch, sha:.headSha[0:12], createdAt}'
```

The run metadata does not expose the `environment` / `command` inputs, so confirm
which environment a run targeted by cross-referencing the Terraform deployment
records for that environment, or by opening the run. `pull_request` / `push`
Terraform runs are always `plan` and never change infrastructure.

```{note}
Terraform state in the Azure backend is the ultimate source of truth. From a
machine with backend access you can read it directly — select the workspace
(`terraform workspace select <env>`) and run `terraform show`. The GitHub trail
above is the answer when you only have repository access.
```

## Check everything at once

The packaged way is [`scripts/show_deployed_versions.sh`](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/scripts/show_deployed_versions.sh)
(see [Quick answer](#quick-answer-the-helper-script) above). The loop it wraps,
for reference — it labels every recent deployment per environment with the
workflow that produced it, separating infra applies from app deploys and
exposing the `ref`/`sha` and timestamp in one pass:

```bash
for env in dev staging prd; do
  echo "===== $env ====="
  for id in $(gh api "repos/{owner}/{repo}/deployments?environment=$env&per_page=5" --jq '.[].id'); do
    url=$(gh api "repos/{owner}/{repo}/deployments/$id/statuses?per_page=1" --jq '.[0].target_url')
    run=$(echo "$url" | grep -oE 'runs/[0-9]+' | grep -oE '[0-9]+')
    wf=$(gh api "repos/{owner}/{repo}/actions/runs/$run" --jq '.name')
    meta=$(gh api "repos/{owner}/{repo}/deployments/$id" --jq '{ref, sha: .sha[0:12], created_at}')
    echo "  [$wf] $meta"
  done
done
```

Read the result with the rules above: `Terraform` rows are infrastructure (only
the `workflow_dispatch`-originated ones are applies); every other workflow is an
app deploy, and for `prd` the version tag must still be read from that run's logs.
```
