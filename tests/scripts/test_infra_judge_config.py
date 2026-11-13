"""Guard the deployed model defaults against silent zero-cost tracking (#259).

An unpriced model does not fail loudly: ``completion_cost`` raises,
``LiteLLMClient`` catches that and sets ``cost = float("nan")``
(``src/qfa/adapters/llm_client.py``), and ``_to_decimal`` coerces NaN to
``Decimal("0")`` (``src/qfa/adapters/tracking_llm.py``) — so every call gets
recorded at zero cost, with only a log line to show for it. Nothing in CI
otherwise checks that the Terraform defaults and the pricing resource agree,
since one is HCL and the other is YAML. These tests cover both the primary
(``llm_model``) and the judge (``judge_llm_model``) default.

The same defaults are the model the docs describe, so the docs below must name
them. The security brief marks its model names with ``data-model`` and
``data-judge-model`` so this check does not depend on the prose. The Terraform
workflow warns when an environment runs a different model.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"
MODEL_PRICES_YAML = REPO_ROOT / "src" / "qfa" / "resources" / "model_prices.yaml"

MODEL_VARIABLES = ("llm_model", "judge_llm_model")

SECURITY_BRIEF = REPO_ROOT / "docs" / "security-brief.html"
BRIEF_ATTRIBUTES = {"llm_model": "data-model", "judge_llm_model": "data-judge-model"}
DOCS_NAMING_MODEL = {
    "llm_model": (
        ".env.example",
        "docs/beginners-guide.md",
        "docs/operations/settings-reference.md",
        "docs/operations/setup-new-env.md",
    ),
    "judge_llm_model": (
        "docs/operations/settings-reference.md",
        "docs/operations/setup-new-env.md",
    ),
}


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


@pytest.mark.parametrize("variable", MODEL_VARIABLES)
def test_security_brief_names_the_default_model(variable: str):
    attribute = BRIEF_ATTRIBUTES[variable]
    found = re.findall(rf'{attribute}="([^"]*)"', SECURITY_BRIEF.read_text())
    assert found == [_model_default(variable)], (
        f"{SECURITY_BRIEF.name} {attribute} is {found}, but {variable} defaults "
        f"to {_model_default(variable)!r}. Update the brief's model text and "
        "attribute together with the default."
    )


@pytest.mark.parametrize(
    ("variable", "doc"),
    [(v, d) for v, docs in DOCS_NAMING_MODEL.items() for d in docs],
)
def test_docs_name_the_default_model(variable: str, doc: str):
    assert _model_default(variable) in (REPO_ROOT / doc).read_text(), (
        f"{doc} does not mention {variable}'s default {_model_default(variable)!r}"
    )


def test_app_default_matches_terraform_default():
    from qfa.settings import LLMSettings

    assert LLMSettings.model_fields["model"].default == _model_default("llm_model")
