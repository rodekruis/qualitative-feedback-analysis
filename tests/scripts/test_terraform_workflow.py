"""Guard that `terraform.yaml` plans and applies with the same variables (#436).

The plan and apply steps each list their `TF_VAR_*` env by hand. When one
list misses a variable, `plan` shows one value and `apply` uses the
Terraform default, without any error. #436 found this for
`TF_VAR_judge_llm_model`: apply ignored `AZ_JUDGE_LLM_MODEL`, so no judge
model change or rollback reached a deployed environment.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
TERRAFORM_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "terraform.yaml"


def _step_env(name: str) -> dict[str, str]:
    workflow = yaml.safe_load(TERRAFORM_WORKFLOW.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step.get("env") or {}
    raise AssertionError(f"no step named {name!r} in {TERRAFORM_WORKFLOW}")


def _tf_vars(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k.startswith("TF_VAR_")}


def test_plan_and_apply_pass_the_same_tf_vars():
    plan = _tf_vars(_step_env("Terraform plan"))
    apply = _tf_vars(_step_env("Terraform apply"))
    assert plan == apply, (
        "the Terraform plan and apply steps must pass the same TF_VAR_* env, "
        "or apply silently uses the default for a variable that plan shows. "
        f"Only in plan: {sorted(plan.keys() - apply.keys())}. "
        f"Only in apply: {sorted(apply.keys() - plan.keys())}. "
        f"Different values: "
        f"{sorted(k for k in plan.keys() & apply.keys() if plan[k] != apply[k])}."
    )
