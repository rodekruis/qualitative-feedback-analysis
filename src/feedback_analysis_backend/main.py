"""Entry point for the feedback analysis backend."""

import uvicorn
from dotenv import find_dotenv, load_dotenv

from feedback_analysis_backend.api.app import create_app
from feedback_analysis_backend.utils import setup_logging

app = create_app()


def main() -> None:
    """Run the application with uvicorn."""
    load_dotenv(find_dotenv())
    setup_logging()

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
