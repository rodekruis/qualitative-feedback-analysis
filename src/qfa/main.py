"""Entry point for the feedback analysis backend."""

import logging

import uvicorn
from dotenv import find_dotenv, load_dotenv

from qfa.api.app import create_app
from qfa.settings import AppSettings, TelemetrySettings
from qfa.utils import setup_logging

logger = logging.getLogger(__name__)

# Initialise Azure Monitor OpenTelemetry when a connection string is configured
# (production/staging). We read it via TelemetrySettings — keeping Pydantic
# Settings the single source of truth rather than pulling the env var out by
# hand — and pass it explicitly to the SDK. TelemetrySettings has no required
# fields, so constructing it here at import time is safe even when the rest of
# the app's environment (LLM keys, DB, ...) is absent — e.g. when Sphinx
# imports this module to autodoc it. Must run before create_app() so FastAPI,
# SQLAlchemy, and httpx are instrumented before their first use. Skipped in
# local dev where the setting is unset.
_telemetry_settings = TelemetrySettings()
if _telemetry_settings.applicationinsights_connection_string:
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=_telemetry_settings.applicationinsights_connection_string.get_secret_value()
    )

app = create_app()


def main() -> None:
    """Run the application with uvicorn."""
    load_dotenv(find_dotenv())
    setup_logging()

    app_settings = AppSettings()
    logger.info("Settings: %s", app_settings.model_dump_json(indent=2))

    network_settings = app_settings.network
    uvicorn.run(
        "qfa.main:app",
        host=network_settings.host,
        port=network_settings.port,
        reload=app_settings.debug,
    )


if __name__ == "__main__":
    main()
