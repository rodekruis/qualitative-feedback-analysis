"""Entry point for the feedback analysis backend."""

import logging

import uvicorn
from dotenv import find_dotenv, load_dotenv

from qfa.api.app import create_app
from qfa.settings import AppSettings
from qfa.telemetry import configure_telemetry, instrument_app
from qfa.utils import setup_logging

logger = logging.getLogger(__name__)

# Telemetry setup runs at module scope because `gunicorn qfa.main:app`
# (entrypoint.sh) gives us no other startup hook. It is a no-op when
# APPLICATIONINSIGHTS_CONNECTION_STRING is unset, so local dev and Sphinx's
# autodoc import are unaffected.
#
# The order below is load-bearing: configure_telemetry() installs the global
# tracer provider, and instrument_app() must run after create_app() (it
# attaches middleware to the instance) but before the first request. See
# qfa.telemetry for why instrumenting the instance is not optional.
_telemetry_enabled = configure_telemetry()

app = create_app()

if _telemetry_enabled:
    instrument_app(app)


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
