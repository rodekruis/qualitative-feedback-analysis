"""Guard the deployed model defaults against silent zero-cost tracking (#259).

An unpriced model does not fail loudly: ``completion_cost`` raises,
``LiteLLMClient`` catches that and sets ``cost = float("nan")``
(``src/qfa/adapters/llm_client.py``), and ``_to_decimal`` coerces NaN to
``Decimal("0")`` (``src/qfa/adapters/tracking_llm.py``) — so every call gets
recorded at zero cost, with only a log line to show for it. Nothing in CI
otherwise checks that the Terraform defaults and the pricing resource agree,
since one is HCL and the other is YAML. These tests cover both the primary
(``llm_model``) and the judge (``judge_llm_model``) default.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"
MODEL_PRICES_YAML = REPO_ROOT / "src" / "qfa" / "resources" / "model_prices.yaml"

MODEL_VARIABLES = ("llm_model", "judge_llm_model")


def _model_default(variable: str) -> str:
    text = VARIABLES_TF.read_text()
    match = re.search(
        rf'variable\s+"{variable}"\s*\{{[^}}]*?default\s*=\s*"([^"]*)"',
        text,
        re.DOTALL,
    )
    assert match, (
        f'could not find `variable "{variable}"` with a `default` in '
        f"{VARIABLES_TF} — has it been renamed or restructured?"
    )
    return match.group(1)


@pytest.mark.parametrize("variable", MODEL_VARIABLES)
def test_model_default_is_a_known_provider_prefix(variable: str):
    default = _model_default(variable)
    assert default, f"{variable}'s default must not be empty"
    assert default.startswith(("azure/", "azure_ai/")), (
        f"{variable} default {default!r} doesn't start with a known "
        "LiteLLM provider prefix (azure/, azure_ai/)"
    )


@pytest.mark.parametrize("variable", MODEL_VARIABLES)
def test_model_default_is_priced(variable: str):
    default = _model_default(variable)
    prices = yaml.safe_load(MODEL_PRICES_YAML.read_text())
    models = (prices or {}).get("models") or {}
    assert default in models, (
        f"{variable}'s Terraform default {default!r} has no entry in "
        f"{MODEL_PRICES_YAML} — every call on the default would be recorded "
        "at zero cost. Add a pricing entry."
    )
