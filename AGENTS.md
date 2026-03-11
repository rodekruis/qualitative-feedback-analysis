# Project Guidelines

## Package Management

Use `uv` for all dependency management (not `pip`). Examples:
- `uv add <package>` to add a dependency
- `uv pip install -e .` to install the project
- `uv run <command>` to run commands in the project environment

## Git

- Github repo: `rodekruis/qualitative-feedback-analysis`
- use conventional commits (https://www.conventionalcommits.org/en/v1.0.0/)
- Commit messages must contain ONLY a conventional commit subject line and optional body. No trailers of any kind.
- DO NOT add "Co-Authored-By" or any AI attribution trailers to commit messages or pull requests.

### Workflow

- if working from a github issue or requested to "follow the feature workflow":
    1. create a new branch from `main`
    2. commit small, focused changes to the branch
    3. open a PR to merge the branch into `main`
    4. close the issue when the PR is merged ("closes #123")
- else: work directly on `main`

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
