"""Guard the deployed judge model against silent zero-cost tracking (#259).

An unpriced model does not fail loudly: ``completion_cost`` raises,
``LiteLLMClient`` catches that and sets ``cost = float("nan")``
(``src/qfa/adapters/llm_client.py``), and ``_to_decimal`` coerces NaN to
``Decimal("0")`` (``src/qfa/adapters/tracking_llm.py``) — so every judge
call gets recorded at zero cost, with only a log line to show for it.
Nothing in CI otherwise checks that the Terraform default and the pricing
resource agree, since one is HCL and the other is YAML. This test is the
only thing standing between a renamed deployment and a silently free judge.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"
MODEL_PRICES_YAML = REPO_ROOT / "src" / "qfa" / "resources" / "model_prices.yaml"

_JUDGE_LLM_MODEL_DEFAULT = re.compile(
    r'variable\s+"judge_llm_model"\s*\{[^}]*?default\s*=\s*"([^"]*)"', re.DOTALL
)


def _judge_llm_model_default() -> str:
    text = VARIABLES_TF.read_text()
    match = _JUDGE_LLM_MODEL_DEFAULT.search(text)
    assert match, (
        'could not find `variable "judge_llm_model"` with a `default` in '
        f"{VARIABLES_TF} — has it been renamed or restructured?"
    )
    return match.group(1)


def test_judge_llm_model_default_is_a_known_provider_prefix():
    default = _judge_llm_model_default()
    assert default, (
        "judge_llm_model's default must not be empty in deployed environments"
    )
    assert default.startswith(("azure/", "azure_ai/")), (
        f"judge_llm_model default {default!r} doesn't start with a known "
        "LiteLLM provider prefix (azure/, azure_ai/)"
    )


def test_judge_llm_model_default_is_priced():
    default = _judge_llm_model_default()
    prices = yaml.safe_load(MODEL_PRICES_YAML.read_text())
    models = (prices or {}).get("models") or {}
    assert default in models, (
        f"judge_llm_model's Terraform default {default!r} has no entry in "
        f"{MODEL_PRICES_YAML} — every judge call on the deployed default "
        "would be recorded at zero cost. Add a pricing entry."
    )
