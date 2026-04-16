"""Entry point for the feedback analysis backend."""

import logging

import uvicorn
from dotenv import find_dotenv, load_dotenv

from qfa.api.app import create_app
from qfa.settings import AppSettings
from qfa.utils import setup_logging

logger = logging.getLogger(__name__)

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
