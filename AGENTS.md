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

- Flow: API call(documents) -> application service -> LLM API -> return result
- Each use case is an application service in `qfa.services`, and each
  route handler depends on the single service it needs. Per-task
  behaviour is selected by the route handler calling the appropriate
  method, not by swapping service implementations (see ADR-011).
  Epic #112 has extracted `SensitivityService`
  (detect_sensitive_content, #263), `CodingService` (assign_codes,
  #265) and `AnalyzeService` (analyze_bulk, analyze_hierarchical,
  #266); `Orchestrator` holds only `summarize_bulk` and `summarize`
  until #267 empties it. Do not add new use cases to it.
- Services share behaviour by **composition only** — the shared LLM-call
  scaffolding is the injected `LLMCallExecutor` collaborator, never a
  base class (see ADR-017).
- Driven adapters (LLM provider, anonymisation) sit behind ports
  declared in `qfa.domain.ports` — for example `LLMPort` and
  `AnonymizationPort` — so implementations can be swapped.
- **Port implementations must explicitly inherit the port** — production
  adapters *and* test doubles (`class LiteLLMClient(LLMPort):`,
  `class FakeLLMPort(LLMPort):`). `Protocol`s don't require it, but the
  explicit base makes the contract navigable in IDEs. Exception: one-line
  `MagicMock(spec=LLMPort)`.
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

Update `docs/` in the same PR as the code — for changes *and* additions. If you
introduce a concept similar in kind to something already covered, extend that page;
start from the [documentation index](docs/README.md). Doc rot is harder to catch in
review than code drift.

If anything security-related changes, update `docs/security-brief.html`.

- Section indexes live at `docs/<section>/index.md` (with thin `README.md` stubs as
  github.com folder landing pages).
- The Sphinx site is built via `make docs` at the repo root; output lands at
  `docs/_build/html/`.

## Documentation and comment style

Brevity is not tidiness — it is what gets the text read. A page or docstring nobody
reads still has to be maintained, so it is worse than none.

The unit of judgement is the *added fact*: after the first line, every line must tell
the reader something they cannot get from the name, the signature, the type hints, or
the code itself.

Earns more than a summary line:

- a contract the signature doesn't show — preconditions, invariants, what it raises
- units, bounds, or formats a type can't carry (`timeout: float` — seconds? ms?)
- caller-visible behaviour that would surprise — mutation, ordering, idempotency,
  cost (an LLM or embedding call), retries
- a pointer to the *why* when it isn't obvious (link the ADR, don't restate it)

Does not:

- restating the signature, or a numpy `Parameters`/`Returns` section where name plus
  type already says it
- narrating the implementation step by step — that's the code
- history ("previously…", "refactored to…") — that's git
- examples for a function whose use is obvious from its name

`docs/` covers behaviour needed to operate or maintain the service; implementation
detail belongs in the code. Prefer a list, table, or short code example over
paragraphs. When editing an existing docstring or page, it must not get longer unless
behaviour was added.
