"""Top-level pytest configuration."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip pydantic-settings-consumed env vars before each test.

    This way, suites run
    the same regardless of the developer's shell, ``.envrc``, or direnv.
    Tests that need specific values still call ``monkeypatch.setenv`` — that
    runs after autouse fixtures, so per-test setup wins as expected.
    """
    # APPLICATIONINSIGHTS_ matters beyond settings hygiene: with it set, the
    # lifespan really instruments the DB engine, which mutates process-global
    # SQLAlchemy state and leaks across tests.
    _SETTINGS_ENV_PREFIXES = (
        "LLM_",
        "ORCHESTRATOR_",
        "AUTH_",
        "DB_",
        "NETWORK_",
        "APPLICATIONINSIGHTS_",
    )
    for key in list(os.environ):
        if key.startswith(_SETTINGS_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
