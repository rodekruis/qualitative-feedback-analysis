# Project Guidelines

## Package Management

Use `uv` for all dependency management (not `pip`). Examples:
- `uv add <package>` to add a dependency
- `uv pip install -e .` to install the project
- `uv run <command>` to run commands in the project environment

## Git Conventions

Use semantic commit messages:

- `feat:` new feature
- `fix:` bug fix (something was actually broken)
- `docs:` documentation changes
- `style:` formatting, missing semicolons, etc. (no code change)
- `refactor:` code restructuring without changing behavior
- `test:` adding or updating tests
- `chore:` maintenance tasks, dependency updates, CI config, cleanup of things that work but are unnecessary

DO NOT add "Co-Authored-By" or any AI attribution trailers to commit messages and pull requests.

## Architecture

Hexagonal architecture. Key concepts:
- Flow: API call(documents) -> Orchestrator -> LLM API -> return result
- The Orchestrator is an exchangeable service behind an interface
- LLM provider is declared via an `LLMPort` so implementations can be swapped
- API calls are authenticated via API keys

## Tech Stack

- FastAPI + uvicorn
- Pydantic for settings and environment loading
- OpenAI API for document analysis

## Testing & Linting

- `make test` to run tests
- `make lint` to run linters
