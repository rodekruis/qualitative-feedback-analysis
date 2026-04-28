"""Shared fixtures for Tier-3 end-to-end API tests.

These tests boot the real FastAPI stack via ``LifespanManager`` so the
startup-time advisory-lock migration and ``TrackingLLMAdapter`` wiring run
exactly as in production. LiteLLM is faked at the HTTP transport layer
via ``respx`` so the real ``LiteLLMClient`` and ``TrackingLLMAdapter`` are
exercised — including ``response_cost`` extraction and exception classes.

Gated by ``@pytest.mark.e2e`` and excluded from the default test run.
Run with ``make db-up && make test-integration``.
"""

from __future__ import annotations

import os

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import integration_db_url

E2E_TENANT_ID = "tenant-e2e"
E2E_API_KEY = "e2e-key"
E2E_SUPER_KEY = "e2e-super"


async def _probe_or_skip(url: str) -> None:
    """Skip the e2e session if Postgres is unreachable."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:
        pytest.skip(
            f"E2E tests require Postgres at {url} (run `make db-up`). "
            f"Connection failed: {exc!s}",
            allow_module_level=True,
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def e2e_db_url() -> str:
    url = integration_db_url()
    await _probe_or_skip(url)
    return url


@pytest_asyncio.fixture
async def e2e_app(e2e_db_url: str, monkeypatch_session):
    """Boot the FastAPI app with full lifespan against the test DB."""
    monkeypatch_session.setenv("DB_TRACK_USAGE", "true")
    monkeypatch_session.setenv("DB_URL", e2e_db_url)

    monkeypatch_session.setenv("LLM_MODEL", "gpt-3.5-turbo")
    monkeypatch_session.setenv("LLM_API_KEY", "fake-test-key")
    monkeypatch_session.setenv("LLM_API_BASE", "")
    monkeypatch_session.setenv("LLM_API_VERSION", "")

    api_keys_json = (
        f'[{{"key_id":"e2e-0","name":"e2e","key":"{E2E_API_KEY}",'
        f'"tenant_id":"{E2E_TENANT_ID}","is_superuser":false}},'
        f'{{"key_id":"e2e-su","name":"e2e-super","key":"{E2E_SUPER_KEY}",'
        f'"tenant_id":"admin","is_superuser":true}}]'
    )
    monkeypatch_session.setenv("AUTH_API_KEYS", api_keys_json)

    from qfa.api.app import create_app

    app = create_app()
    async with LifespanManager(app):
        # TRUNCATE between tests so each run starts clean.
        engine = create_async_engine(e2e_db_url)
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE TABLE llm_calls RESTART IDENTITY"))
        await engine.dispose()
        yield app


@pytest_asyncio.fixture
async def e2e_client(e2e_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=e2e_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
def monkeypatch_session(monkeypatch):
    """Alias for ``monkeypatch`` to make env-mutation intent explicit.

    Pytest's built-in ``monkeypatch`` is function-scoped; we keep that scope
    so each test gets a fresh env, but rename for readability inside the
    e2e fixtures.
    """
    return monkeypatch


@pytest_asyncio.fixture
async def e2e_engine(e2e_db_url: str):
    engine = create_async_engine(e2e_db_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def openai_chat_response():
    """A canonical OpenAI chat completion JSON body."""

    def _make(
        text: str = "ok",
        prompt_tokens: int = 5,
        completion_tokens: int = 2,
        model: str = "gpt-3.5-turbo",
    ) -> dict:
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    return _make


# Avoid leaking the integration conftest's pg_url fixture by reimporting it
# (pytest discovers fixtures across conftests within the same package tree).
__all__ = [
    "E2E_API_KEY",
    "E2E_SUPER_KEY",
    "E2E_TENANT_ID",
    "e2e_app",
    "e2e_client",
    "e2e_db_url",
    "e2e_engine",
    "openai_chat_response",
]


# Make INTEGRATION_DB_URL discoverable as the same DB:
os.environ.setdefault("INTEGRATION_DB_URL", integration_db_url())
