"""Shared fixtures for Tier-3 end-to-end API tests.

These tests boot the real FastAPI stack via ``LifespanManager`` so the
``TrackingLLMAdapter`` wiring runs exactly as in production. Schema
migrations are run once by the session-scoped ``pg_engine`` fixture
(``qfa.cli.migrate.run_migrations``) — production runs the same code via
``entrypoint.sh`` before the app starts, so the lifespan itself does not
touch Alembic. The LLM port is injected via ``create_app``'s
``llm_factory`` parameter — no monkeypatching, no respx;
``TrackingLLMAdapter`` still wraps the fake exactly as it would wrap the
real client.

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

from qfa.domain.models import LLMResponse
from tests.integration.conftest import integration_db_url

E2E_TENANT_ID = "tenant-e2e"
E2E_API_KEY = "e2e-key"
E2E_SUPER_KEY = "e2e-super"


class FakeLLMPort:
    """Queue-based ``LLMPort`` fake for e2e tests.

    Each ``complete()`` call pops the next queued item. Items are either
    ``LLMResponse`` (returned) or ``Exception`` (raised). An empty queue
    raises ``AssertionError`` so unexpected calls fail loudly.
    """

    def __init__(self) -> None:
        self._queued: list[LLMResponse | Exception] = []
        self.calls: list[dict] = []

    def queue_response(self, response: LLMResponse) -> None:
        self._queued.append(response)

    def queue_failure(self, exc: Exception) -> None:
        self._queued.append(exc)

    def queue_default_response(
        self,
        text: str = "ok",
        model: str = "gpt-3.5-turbo",
        prompt_tokens: int = 5,
        completion_tokens: int = 2,
        cost: float = 0.0001,
    ) -> None:
        self._queued.append(
            LLMResponse(
                text=text,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
            )
        )

    async def complete(
        self,
        system_message: str,
        user_message: str,
        timeout: float,
        tenant_id: str,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system_message": system_message,
                "user_message": user_message,
                "timeout": timeout,
                "tenant_id": tenant_id,
            }
        )
        if not self._queued:
            raise AssertionError(
                "FakeLLMPort.complete called with no queued response/failure"
            )
        item = self._queued.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


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


@pytest_asyncio.fixture(scope="session")
async def e2e_migrated(e2e_db_url: str) -> str:
    """Run ``alembic upgrade head`` once per e2e session.

    The app lifespan no longer runs migrations (those moved to
    ``entrypoint.sh`` in production), so e2e tests must bring the schema
    up themselves before booting the app.
    """
    from qfa.cli.migrate import run_migrations

    await run_migrations(e2e_db_url)
    return e2e_db_url


@pytest_asyncio.fixture
async def e2e_app(e2e_migrated: str, monkeypatch):
    e2e_db_url = e2e_migrated
    """Boot the FastAPI app with a FakeLLMPort wired via ``create_app``."""
    monkeypatch.setenv("DB_TRACK_USAGE", "true")
    monkeypatch.setenv("DB_URL", e2e_db_url)

    # The fake LLM ignores model/api_base, but settings still need to validate.
    monkeypatch.setenv("LLM_MODEL", "gpt-3.5-turbo")
    monkeypatch.setenv("LLM_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_API_BASE", "")
    monkeypatch.setenv("LLM_API_VERSION", "")

    api_keys_json = (
        f'[{{"key_id":"e2e-0","name":"e2e","key":"{E2E_API_KEY}",'
        f'"tenant_id":"{E2E_TENANT_ID}","is_superuser":false}},'
        f'{{"key_id":"e2e-su","name":"e2e-super","key":"{E2E_SUPER_KEY}",'
        f'"tenant_id":"admin","is_superuser":true}}]'
    )
    monkeypatch.setenv("AUTH_API_KEYS", api_keys_json)

    from qfa.api.app import create_app

    fake_llm = FakeLLMPort()
    app = create_app(llm_factory=lambda _settings: fake_llm)

    async with LifespanManager(app):
        engine = create_async_engine(e2e_db_url)
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE TABLE llm_calls RESTART IDENTITY"))
        await engine.dispose()
        yield app, fake_llm


@pytest_asyncio.fixture
async def e2e_client(e2e_app):
    app, _fake = e2e_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def e2e_fake_llm(e2e_app) -> FakeLLMPort:
    _app, fake = e2e_app
    return fake


@pytest_asyncio.fixture
async def e2e_engine(e2e_db_url: str):
    engine = create_async_engine(e2e_db_url)
    yield engine
    await engine.dispose()


__all__ = [
    "E2E_API_KEY",
    "E2E_SUPER_KEY",
    "E2E_TENANT_ID",
    "FakeLLMPort",
    "e2e_app",
    "e2e_client",
    "e2e_db_url",
    "e2e_engine",
    "e2e_fake_llm",
]


# Make the integration DB URL discoverable to the integration conftest fixtures
# this conftest reuses (it imports ``integration_db_url`` directly).
os.environ.setdefault("INTEGRATION_DB_URL", integration_db_url())
