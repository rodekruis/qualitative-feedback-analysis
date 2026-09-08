"""Azure Monitor / OpenTelemetry bootstrap.

Split out of :mod:`qfa.main` so the wiring is testable and so the ordering
constraints below are stated once, in one place, instead of living as
import-time side effects.

Four independent things have to happen for the Application Insights ``App*``
tables to populate, so they fail independently -- one empty table says nothing
about the others (#223):

============================ ==============================================
Table                        Populated by
============================ ==============================================
``AppRequests``,             :func:`instrument_app` -- the FastAPI ASGI
``AppExceptions``            middleware, attached to the app *instance*
``AppDependencies``          :func:`configure_telemetry` (aiohttp +
                             httpx, for the LLM) +
                             :func:`instrument_db_engine` (SQLAlchemy, for
                             Postgres)
``AppTraces``                the SDK's root-logger handler, which
                             :func:`qfa.utils.setup_logging` must preserve
============================ ==============================================
"""

import logging
from typing import TYPE_CHECKING

from qfa.settings import TelemetrySettings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.trace import TracerProvider
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


def configure_telemetry(settings: TelemetrySettings | None = None) -> bool:
    """Initialise the Azure Monitor SDK and instrument outbound HTTP clients.

    Returns ``False`` without touching global state when no connection string
    is configured -- the local-dev case. Otherwise this mutates process-global
    state (tracer/logger/meter providers, and the ``httpx``/``aiohttp`` client
    internals) and returns ``True``.

    Must be called before :func:`instrument_app` and
    :func:`instrument_db_engine`, which resolve a tracer from the global
    provider this installs.

    Both HTTP client libraries are instrumented, and neither is redundant:
    litellm's *default* transport is ``LiteLLMAiohttpTransport``, which talks
    to aiohttp directly and so bypasses httpx's transport entirely -- LLM
    calls are missing from ``AppDependencies`` without the aiohttp
    instrumentation. litellm's own housekeeping requests (the model-price
    list) do go over httpx.

    Both are process-global and order-safe: they wrap class attributes
    (``httpx.HTTPTransport.handle_request`` /
    ``AsyncHTTPTransport.handle_async_request``, and
    ``aiohttp.ClientSession._request``), so clients litellm already built at
    import time are traced too.
    """
    resolved = settings if settings is not None else TelemetrySettings()
    connection_string = resolved.applicationinsights_connection_string
    if connection_string is None:
        logger.debug(
            "APPLICATIONINSIGHTS_CONNECTION_STRING unset; telemetry export disabled"
        )
        return False

    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    configure_azure_monitor(connection_string=connection_string.get_secret_value())
    AioHttpClientInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    logger.info("Azure Monitor telemetry configured")
    return True


def instrument_app(
    app: "FastAPI", *, tracer_provider: "TracerProvider | None" = None
) -> None:
    """Attach the OpenTelemetry ASGI middleware to a FastAPI instance.

    Call this after ``create_app()`` and before the app serves its first
    request: it overrides ``build_middleware_stack`` on the instance, and
    Starlette builds that stack lazily on the first request.

    Do **not** rely on ``FastAPIInstrumentor().instrument()`` instead. That
    form rebinds the ``fastapi.FastAPI`` module attribute, so it only affects
    modules that resolve the class *after* it runs -- and
    :mod:`qfa.api.app` from-imports ``FastAPI`` at its own import time. That
    is why the ``App*`` tables were empty in #223.

    Idempotent: the instrumentation guards on the app's
    ``_is_instrumented_by_opentelemetry`` flag.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)


def instrument_db_engine(
    engine: "AsyncEngine", *, tracer_provider: "TracerProvider | None" = None
) -> None:
    """Emit ``AppDependencies`` spans for statements run on ``engine``.

    Instruments this engine only. The argument-less
    ``SQLAlchemyInstrumentor().instrument()`` would instead patch
    ``sqlalchemy.ext.asyncio.create_async_engine``, which
    :mod:`qfa.adapters.db` from-imports -- the same rebind trap described in
    :func:`instrument_app`.

    Spans carry the *parameterised* statement in ``db.statement``; bound
    parameter values are never recorded.

    Note this instruments SQLAlchemy, not asyncpg.
    ``opentelemetry-instrumentation-asyncpg`` looks like the closer fit but
    wraps only ``Connection.execute``/``fetch``/``fetchrow``/..., whereas
    SQLAlchemy's asyncpg dialect issues statements via
    ``Connection.prepare()`` + ``PreparedStatement.fetch()`` -- so it would
    install cleanly and produce almost no spans.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine, tracer_provider=tracer_provider
    )
