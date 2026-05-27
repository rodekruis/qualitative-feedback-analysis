# Project Guidelines

## Package Management

Use `uv` for all dependency management (not `pip`). Examples:

- `uv add <package>` to add a dependency
- `uv pip install -e .` to install the project
- `uv run <command>` to run commands in the project environment

## Git

- Github repo: `rodekruis/qualitative-feedback-analysis`
- use conventional commits (https://www.conventionalcommits.org/en/v1.0.0/)
- Commit messages must contain ONLY a conventional commit subject line and optional
  body. No trailers of any kind.

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
- The Orchestrator is a single application service composed of multiple
  use cases (analyze, summarize, summarize_aggregate, assign_codes).
  Per-task behaviour is selected by the route handler calling the
  appropriate method, not by swapping orchestrator implementations
  (see ADR-011).
- Driven adapters (LLM provider, anonymisation) sit behind ports
  declared in `qfa.domain.ports` — for example `LLMPort` and
  `AnonymizationPort` — so implementations can be swapped.
- **Every class that implements a port must explicitly inherit from it
  — by default, this applies to production adapters *and* test
  doubles** (e.g. `class LiteLLMClient(LLMPort):`,
  `class PresidioAnonymizer(AnonymizationPort):`,
  `class FakeLLMPort(LLMPort):`). Although Python `Protocol`s support
  structural typing without inheritance, the explicit base class makes
  the port↔adapter relationship discoverable in IDEs ("go to
  definition" jumps to the contract) and signals intent to readers.
  Skipping the inheritance is reserved for genuinely ad-hoc cases
  (e.g. one-line `unittest.mock.MagicMock(spec=LLMPort)` usages, which
  enforce conformance via `spec=`) and should be the exception, not
  the default.
- API calls are authenticated via API keys.

Layer rules are enforced by `import-linter` contracts in
`pyproject.toml` (`make lint` runs them). The hexagonal package
layout is:

- `qfa.domain` — entities, value objects, errors, and driven ports
  (the inner core; no third-party infrastructure imports).
- `qfa.services` — application services / use cases (orchestrator and
  pure helpers; depends on `qfa.domain`).
- `qfa.adapters` — driven adapter implementations of ports declared
  in `qfa.domain.ports` (LiteLLM, Presidio, etc.).
- `qfa.api` — driving adapter (FastAPI routes, dependencies, app
  composition). `qfa.api.app` is the composition root that wires
  adapters into the orchestrator at startup.

## Tech Stack

- FastAPI + uvicorn
- Pydantic for settings and environment loading
- OpenAI API for document analysis

## Testing & Linting

- `make test` to run tests
- `make lint` to run linters

## Documentation

Keep `docs/` in sync with code changes. When a change touches anything documented under
`docs/` — architecture, ports/adapters, settings, endpoints, operational procedures, the
developer workflow — update the relevant page in the same PR. This also applies to
*additions*: if you introduce a new concept or behavior that's similar in kind to what
`docs/` already covers, add or extend the relevant page in the same PR -- start from
the [documentation index](docs/README.md) to see what's covered. Doc rot is harder to
catch in review than code drift; the cheapest moment to fix it is while the change is
fresh.

- Section indexes live at `docs/<section>/index.md` (with thin `README.md` stubs as
  github.com folder landing pages).
- The Sphinx site is built via `make docs` at the repo root; output lands at
  `docs/_build/html/`.
