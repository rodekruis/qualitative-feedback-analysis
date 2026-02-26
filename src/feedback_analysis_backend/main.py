"""Entry point for the feedback analysis backend."""

import uvicorn

from feedback_analysis_backend.api.app import create_app

app = create_app()


def main() -> None:
    """Run the application with uvicorn."""
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
