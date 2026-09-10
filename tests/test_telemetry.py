"""Tests for the Azure Monitor / OpenTelemetry bootstrap.

Why: the App Insights ``App*`` tables sat empty for an entire release even
though ``configure_azure_monitor()`` was being called correctly (#223). Every
failure was invisible — no exception, no log line, just no telemetry. So these
tests assert the *observable* result (a span exists, a handler is still
attached) rather than that a setup function was called.

The instrumentations mutate process-global state, so every test that
instruments must undo it in teardown or it leaks into the rest of the suite.
"""

from __future__ import annotations

from collections.abc import Iterator

import fastapi
import httpx
import pytest
import sqlalchemy as sa
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from qfa.api.app import create_app
from qfa.settings import TelemetrySettings
from qfa.telemetry import configure_telemetry, instrument_app, instrument_db_engine

CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
FAKE_CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.invalid/"
)


@pytest.fixture
def exporter() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    """A tracer provider that collects finished spans in memory.

    Deliberately *not* installed as the global provider — it is passed
    explicitly so tests stay independent of global provider state, which
    OpenTelemetry only lets you set once per process.
    """
    span_exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    yield provider, span_exporter
    provider.shutdown()


@pytest.fixture
def uninstrument_http_clients() -> Iterator[None]:
    """Undo the client instrumentations, which patch library-global classes."""
    yield
    HTTPXClientInstrumentor().uninstrument()
    AioHttpClientInstrumentor().uninstrument()


@pytest.fixture
def instrumented_app(
    exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> Iterator[tuple[fastapi.FastAPI, InMemorySpanExporter]]:
    """A real application instance with the ASGI middleware attached."""
    provider, span_exporter = exporter
    app = create_app()
    instrument_app(app, tracer_provider=provider)
    yield app, span_exporter
    FastAPIInstrumentor.uninstrument_app(app)


def test_configure_telemetry_is_a_noop_without_a_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dev has no connection string, and must not pay for an SDK setup."""
    monkeypatch.delenv(CONNECTION_STRING_ENV, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        "azure.monitor.opentelemetry.configure_azure_monitor",
        lambda **kwargs: calls.append(kwargs),
    )

    assert configure_telemetry() is False
    assert calls == []
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False
    assert AioHttpClientInstrumentor().is_instrumented_by_opentelemetry is False


def test_configure_telemetry_passes_the_connection_string_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch, uninstrument_http_clients: None
) -> None:
    """The connection string reaches the SDK unwrapped from its ``SecretStr``."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "azure.monitor.opentelemetry.configure_azure_monitor",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setenv(CONNECTION_STRING_ENV, FAKE_CONNECTION_STRING)

    assert configure_telemetry() is True
    assert calls == [{"connection_string": FAKE_CONNECTION_STRING}]


def test_configure_telemetry_instruments_both_http_clients(
    monkeypatch: pytest.MonkeyPatch, uninstrument_http_clients: None
) -> None:
    """Outbound LLM calls need *both* client instrumentations.

    The distro bundles neither — its auto-instrumentation list covers only
    azure_sdk, django, fastapi, flask, psycopg2, requests, urllib and urllib3.

    aiohttp is the one that actually carries the LLM request: litellm's default
    transport is ``LiteLLMAiohttpTransport``, which reaches aiohttp directly
    and never goes through httpx's transport. httpx still carries litellm's
    housekeeping calls, so dropping either one loses dependency records.
    """
    monkeypatch.setattr(
        "azure.monitor.opentelemetry.configure_azure_monitor", lambda **kwargs: None
    )

    configure_telemetry(
        TelemetrySettings(
            applicationinsights_connection_string=SecretStr(FAKE_CONNECTION_STRING)
        )
    )

    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True
    assert AioHttpClientInstrumentor().is_instrumented_by_opentelemetry is True


@pytest.mark.asyncio
async def test_litellms_default_transport_is_not_traced_by_httpx_alone() -> None:
    """Guard the reason aiohttp instrumentation is in the dependency list.

    If a future litellm switches back to an httpx-native transport this test
    fails, which is the signal to re-evaluate the extra dependency rather than
    carry it forever. Asserting on the transport type keeps that check cheap
    and offline — no LLM call, no network.
    """
    from litellm.llms.custom_httpx.aiohttp_transport import LiteLLMAiohttpTransport
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    handler = AsyncHTTPHandler()
    try:
        assert isinstance(handler.client._transport, LiteLLMAiohttpTransport)
    finally:
        await handler.client.aclose()


def test_instrument_app_does_not_depend_on_the_fastapi_class_patch(
    instrumented_app: tuple[fastapi.FastAPI, InMemorySpanExporter],
) -> None:
    """Instrumentation attaches to the instance, not via the class swap (#223).

    ``FastAPIInstrumentor().instrument()`` works by rebinding the
    ``fastapi.FastAPI`` module attribute, which does nothing for
    :mod:`qfa.api.app` — it from-imported ``FastAPI`` at its own import time
    and instantiates that original class. That is exactly why ``AppRequests``
    was empty. Asserting the class was *never* swapped is what makes this a
    regression test and not a restatement of the implementation.
    """
    app, _ = instrumented_app

    assert type(app) is fastapi.FastAPI
    # Set dynamically by the instrumentation, hence getattr.
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True


@pytest.mark.asyncio
async def test_instrumented_app_records_a_server_span_per_request(
    instrumented_app: tuple[fastapi.FastAPI, InMemorySpanExporter],
) -> None:
    """A request through the real ASGI stack produces one ``AppRequests`` span.

    ``/v1/health`` needs nothing from ``app.state``, so this runs without the
    lifespan — and it is the endpoint the platform health probe hits, which is
    what makes an idle environment show a non-zero request rate.
    """
    app, span_exporter = instrumented_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200

    server_spans = [
        s for s in span_exporter.get_finished_spans() if s.kind is SpanKind.SERVER
    ]
    assert len(server_spans) == 1
    assert server_spans[0].attributes is not None
    assert server_spans[0].attributes["http.route"] == "/v1/health"


@pytest.mark.asyncio
async def test_instrument_app_is_idempotent(
    instrumented_app: tuple[fastapi.FastAPI, InMemorySpanExporter],
) -> None:
    """A second call must not double-wrap the middleware stack.

    Double-wrapping would not raise — it would silently double every request
    record in App Insights, and double the ingestion bill with it.
    """
    app, span_exporter = instrumented_app

    instrument_app(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/v1/health")

    server_spans = [
        s for s in span_exporter.get_finished_spans() if s.kind is SpanKind.SERVER
    ]
    assert len(server_spans) == 1


@pytest.mark.asyncio
async def test_sqlalchemy_spans_do_not_record_bound_parameters(
    exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    """Dependency spans carry parameterised SQL only — never the values.

    Bound parameters in this app's queries include tenant identifiers and
    hashed API keys, and App Insights is a far wider audience than the
    database. Run against SQLite so the guard needs no Postgres; the
    instrumentation records ``db.statement`` the same way for either dialect.
    """
    provider, span_exporter = exporter
    sentinel = "tenant-must-not-be-exported"
    engine = create_async_engine("sqlite+aiosqlite://")
    instrument_db_engine(engine, tracer_provider=provider)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE t (tenant_id TEXT)"))
            await conn.execute(
                sa.text("INSERT INTO t (tenant_id) VALUES (:tenant_id)"),
                {"tenant_id": sentinel},
            )
    finally:
        SQLAlchemyInstrumentor().uninstrument()
        await engine.dispose()

    spans = span_exporter.get_finished_spans()
    statements = [
        s.attributes["db.statement"]
        for s in spans
        if s.attributes is not None and "db.statement" in s.attributes
    ]
    assert any("INSERT INTO t" in str(stmt) for stmt in statements), statements
    for span in spans:
        for value in (span.attributes or {}).values():
            assert sentinel not in str(value)
