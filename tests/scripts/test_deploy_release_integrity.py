"""Guard the digest-integrity choke point added for #173.

`_deploy-release.yaml` is the single reusable workflow all five deploy paths
funnel through (release -> dev, promote-to-dev/staging/prd, auto-staging-on-
publish). None of this logic runs in CI -- only in anger, on the exact event
#173 is about -- so a step reorder, a dropped `needs`, or a reintroduced grep
on the release body would otherwise land silently. See ADR-023.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_EVENT_CONTROLLED_NEEDLES = ("inputs.", "github.event", "github.head_ref")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else needs


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _step_index(steps: list[dict], predicate) -> int:
    for i, step in enumerate(steps):
        if predicate(step):
            return i
    raise AssertionError(f"no step matched among {[s.get('name') for s in steps]!r}")


def test_deploy_resolves_the_digest_from_the_registry():
    """The deploy job reads the digest from ACR, not by grepping the release body."""
    deploy = _load("_deploy-release.yaml")["jobs"]["deploy"]
    steps = _steps(deploy)
    runs = [s.get("run") or "" for s in steps]
    assert any("az acr repository show" in run for run in runs)
    assert not any(re.search(r"grep.*sha256", run) for run in runs), (
        "digest must not be extracted with a body grep -- see #173"
    )


def test_deploy_verifies_the_release_body_against_the_registry():
    """A verification step runs the cross-check script before the digest is deployed."""
    deploy = _load("_deploy-release.yaml")["jobs"]["deploy"]
    steps = _steps(deploy)
    verify_index = _step_index(
        steps, lambda s: "verify_release_digest.py" in (s.get("run") or "")
    )
    deploy_index = _step_index(
        steps, lambda s: "az webapp config container set" in (s.get("run") or "")
    )
    assert verify_index < deploy_index


def test_azure_login_precedes_digest_resolution():
    """Azure login must run before the ACR digest lookup, or it runs unauthenticated."""
    deploy = _load("_deploy-release.yaml")["jobs"]["deploy"]
    steps = _steps(deploy)
    login_index = _step_index(
        steps, lambda s: (s.get("uses") or "").startswith("azure/login")
    )
    resolve_index = _step_index(
        steps, lambda s: "az acr repository show" in (s.get("run") or "")
    )
    assert login_index < resolve_index


def test_deploy_checks_out_the_verification_script_from_the_default_branch():
    """The verification script is read from main, not the release's own (possibly stale) commit."""
    deploy = _load("_deploy-release.yaml")["jobs"]["deploy"]
    checkout = _steps(deploy)[0]
    assert (checkout.get("uses") or "").startswith("actions/checkout")
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"


def test_auto_staging_verifies_before_deploying():
    """auto-staging-on-publish gains a verify job that both terraform and deploy depend on."""
    jobs = _load("auto-staging-on-publish.yaml")["jobs"]
    assert "verify" in jobs
    assert "verify" in _needs(jobs["terraform"])
    assert "verify" in _needs(jobs["deploy"])


def test_no_event_controlled_interpolation_in_run_blocks():
    """No `run:` step interpolates attacker-influenced input directly into shell.

    `inputs.*`, `github.event.*`, and `github.head_ref` must be bound to `env:`
    and read back via `"$VAR"` -- never spliced straight into the script by the
    Actions template engine. #173's own vulnerability was a variant of this
    class (an editable value trusted as a deploy identity); this is the
    durable, repo-wide guard.
    """
    violations = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in _steps(job):
                run = step.get("run")
                if not run:
                    continue
                for expr in _EXPR_RE.findall(run):
                    if any(needle in expr for needle in _EVENT_CONTROLLED_NEEDLES):
                        violations.append(
                            f"{path.name}: job {job_name!r}, "
                            f"step {step.get('name')!r}: ${{{{{expr}}}}}"
                        )
    assert not violations, "\n".join(violations)
