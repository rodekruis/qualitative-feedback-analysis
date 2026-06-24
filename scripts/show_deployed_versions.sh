#!/usr/bin/env bash
#
# Show the application version and infrastructure commit currently deployed to
# each environment, using only the GitHub paper trail — no Azure access needed.
#
# How it works:
#   - Every app deploy and Terraform run declares a GitHub `environment`, so
#     GitHub records a deployment per run. We read the deployments for each
#     environment and follow each one to the workflow that created it.
#   - App deploys (Release / Promote to * / Auto-deploy ...) record their
#     version in the deployment status `description` (e.g. "v2.0.0 @ sha256:..."),
#     written by .github/workflows/_deploy-release.yaml.
#   - Infra applies come from the `Terraform` workflow via `workflow_dispatch`;
#     Terraform `plan` runs (pull_request / push) are skipped — they change
#     nothing. The deployed infra "version" is the run's commit SHA.
#
# For the live app version when the service is up, prefer `GET /v1/health`.
# When the app is down, also check the Azure tags directly:
#   az webapp show -g <resource-group> -n <app-name> --query tags
#
# Usage:   scripts/show_deployed_versions.sh [owner/repo]
# Requires: gh (authenticated) and jq.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
ENVIRONMENTS=(dev staging prd)

# target_url looks like ".../actions/runs/<run>/job/<job>" — extract <run>.
run_id_from_url() {
  printf '%s' "$1" | grep -oE 'runs/[0-9]+' | grep -oE '[0-9]+' | head -n1
}

for env in "${ENVIRONMENTS[@]}"; do
  printf '== %s ==\n' "$env"
  app_done=false
  infra_done=false

  for id in $(gh api "repos/$REPO/deployments?environment=$env&per_page=20" --jq '.[].id'); do
    $app_done && $infra_done && break

    target_url=$(gh api "repos/$REPO/deployments/$id/statuses?per_page=1" --jq '.[0].target_url // ""')
    run_id=$(run_id_from_url "$target_url")
    run_meta=$(gh api "repos/$REPO/actions/runs/$run_id" --jq '"\(.name)|\(.event)"' 2>/dev/null || echo '?|?')
    workflow=${run_meta%%|*}
    event=${run_meta##*|}
    meta=$(gh api "repos/$REPO/deployments/$id" --jq '"\(.created_at)  ref=\(.ref)  sha=\(.sha[0:12])"')

    if [ "$workflow" = "Terraform" ]; then
      # Skip plans (pull_request / push) — only manual dispatches apply.
      [ "$event" = "workflow_dispatch" ] || continue
      $infra_done && continue
      printf '  infra: %s  (Terraform apply)\n' "$meta"
      infra_done=true
    else
      $app_done && continue
      version=$(gh api "repos/$REPO/deployments/$id/statuses" \
        --jq 'first(.[] | select(.description != "") | .description) // "<no version recorded>"')
      printf '  app:   %s  [%s]  %s\n' "$version" "$workflow" "$meta"
      app_done=true
    fi
  done
done
