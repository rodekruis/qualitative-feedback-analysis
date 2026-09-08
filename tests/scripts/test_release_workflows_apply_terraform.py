"""Guard the dev/staging infra-currency gate added for #224.

`release.yaml` (auto-deploy to dev) and `auto-staging-on-publish.yaml`
(auto-deploy to staging) each run a `terraform` job with `command: apply`
for their target environment, and the deploy job depends on it. Nothing
else in CI checks this wiring, and it is easy to lose silently: someone
adding a step to either workflow could reorder the jobs, or a future
edit could drop `terraform` from `deploy`'s `needs`, without breaking any
existing test. Either regression reopens the exact gap #224 was raised
for — a release reaching dev or staging ahead of the infrastructure it
depends on.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else needs


def test_release_applies_terraform_to_dev_before_deploying():
    jobs = _load("release.yaml")["jobs"]
    terraform = jobs["terraform"]
    assert terraform["with"]["environment"] == "dev"
    assert terraform["with"]["command"] == "apply"
    assert "terraform" in _needs(jobs["deploy-to-dev"]), (
        "deploy-to-dev must depend on the terraform job, or dev can deploy "
        "ahead of its own infrastructure again"
    )


def test_auto_staging_on_publish_applies_terraform_to_staging_before_deploying():
    jobs = _load("auto-staging-on-publish.yaml")["jobs"]
    terraform = jobs["terraform"]
    assert terraform["with"]["environment"] == "staging"
    assert terraform["with"]["command"] == "apply"
    assert "terraform" in _needs(jobs["deploy"]), (
        "deploy must depend on the terraform job, or staging can deploy "
        "ahead of its own infrastructure again"
    )
