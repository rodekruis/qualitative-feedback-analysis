# Developer guide

Everything you need to go from a fresh clone to a green `make test`.

Prefer a container? The devcontainer is an optional, per-developer secure Claude Code sandbox — it installs the project with uv, wires up pre-commit, and adds a default-deny egress firewall. It is no longer shipped in this repo; inject it locally from the [copier template](https://github.com/mariushelf/copier-devcontainer) with `uvx copier copy gh:mariushelf/copier-devcontainer "$(pwd)"`. The rest of this page assumes you're setting up directly on your host.

## 1. Check out the code

```bash
git clone git@github.com:rodekruis/qualitative-feedback-analysis.git
cd qualitative-feedback-analysis
```

Make a branch off `main` for any non-trivial work (see [Project workflow](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/AGENTS.md#workflow)).

## 2. Set up your local environment

You can use [direnv](https://direnv.net/) to load per-project environment variables automatically when you `cd` into the repo. This repo no longer ships a `.envrc` (the optional devcontainer brings its own), so for a host setup add a one-line `.envrc` that sources `.env` if you want direnv to load it automatically.

```bash
# install direnv (macOS: brew install direnv; Debian/Ubuntu: apt install direnv)
# then hook it into your shell — see https://direnv.net/docs/hook.html

cp .env.example .env
$EDITOR .env                 # fill in the values you actually need locally
echo 'dotenv .env' > .envrc  # tell direnv to load .env (repo no longer ships this)
direnv allow                 # one-time approval for this directory
```

`.env.example` is the starter template — copy it, edit it, **never commit `.env`** (it's gitignored). Required variables and defaults are listed in [Settings reference](../operations/settings-reference.md).

If you'd rather not use direnv, export the same variables manually or load `.env` from your shell profile. The application itself reads settings via pydantic-settings, so any mechanism that puts the variables into the process environment works.

## 3. Install dependencies and hooks

```bash
uv sync                       # creates .venv/ and installs project + dev deps
uv run pre-commit install     # wires pre-commit into .git/hooks/pre-commit
```

`pre-commit install` only needs to run once per clone — after that, the hooks fire on every `git commit`. To run them ad-hoc:

```bash
make pre_commit               # runs all hooks on all files
uv run pre-commit run         # runs hooks on staged files only
```

The configured hooks are in `.pre-commit-config.yaml`: ruff (lint + format), yamllint, nbstripout for notebooks, `ty` type-checking, and `lint-imports` for the hexagonal layer contracts.

## 4. Verify the setup

```bash
make test                     # unit tier — should pass on a fresh clone
make lint                     # ruff + ty + import-linter
```

## Running the full test suite

The suite is split into three tiers. The default `make test` runs only the fast unit tier; integration and e2e are gated behind pytest markers and a running Postgres.

| Tier | Marker | Needs | Command |
|------|--------|-------|---------|
| Unit | (none) | — | `make test` |
| Integration | `integration` | Postgres | `make db-up && make test-integration` |
| E2E | `e2e` | Postgres | `make db-up && make test-integration` |

`make test-integration` runs both `integration` and `e2e` markers in one pass. The first invocation also runs `alembic upgrade head` once via the session-scoped `pg_engine` fixture.

All three tiers run in CI on every push: unit in the `test` job, integration + e2e in a dedicated `integration` job that brings up a Postgres 16 service container (see `.github/workflows/ci.yaml`).

### Postgres for tiers 2 and 3

```bash
make db-up        # start docker-compose Postgres on localhost:5432
make db-down      # stop the container, keep the volume (fast restart)
make db-reset     # nuke the volume and start fresh (~5s)
make migrate      # apply migrations manually (rarely needed; tests do this)
```

The default URL is `postgresql+asyncpg://qfa:qfa@localhost:5432/qfa`. Point at a different host with the `INTEGRATION_DB_URL` env var:

```bash
INTEGRATION_DB_URL=postgresql+asyncpg://user:pw@host:5432/db make test-integration
```

### Running a specific tier or test

```bash
uv run pytest -m integration                          # tier 2 only
uv run pytest -m e2e                                  # tier 3 only
uv run pytest tests/integration/test_db_postgres.py   # specific file
```

## Coding style and conventions

- **Follow the [project guidelines](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/AGENTS.md).** They cover package management (`uv`, not `pip`), commit messages (conventional commits), and the hexagonal layer rules.
- **Every class that implements a port explicitly inherits from it — production adapters *and* test doubles.** Even though Python `Protocol`s support structural typing, we require `class LiteLLMClient(LLMPort):`, `class PresidioAnonymizer(AnonymizationPort):`, and `class FakeLLMPort(LLMPort):` so that "go to definition" in an IDE jumps from any port impl to its contract. Skipping the inheritance is reserved for genuinely ad-hoc cases (e.g. one-shot `MagicMock(spec=Port)` instances, where `spec=` already enforces conformance). See [Components](../architecture/03-components.md) for the full ports/adapters layout.
- **Import directions are enforced.** `qfa.domain` must not import third-party infra (`litellm`, `presidio_*`, `fastapi`, ...); `qfa.services` may only import `qfa.domain`; the composition root in `qfa.api.app` is the only place that wires concrete adapters into ports. `make lint` runs `lint-imports` to enforce this.

## How-to guides

Task-oriented guides for common development work, walking through a change end
to end across the hexagonal layers.

- [Implementing a new endpoint](implementing-a-new-endpoint.md) — adding an HTTP endpoint: domain and API models, the orchestrator use case, authentication, and usage tracking.

## Where to go next

- [Architecture overview](../architecture/01-architecture-style.md) — hexagonal layout, why we picked it
- [Components](../architecture/03-components.md) — ports, adapters, orchestrator, composition root
- [Settings reference](../operations/settings-reference.md) — every environment variable
- [REST API overview](../rest-api/index.md) — HTTP endpoints and request shapes
- [Python API reference](../python-api/index.md) — auto-generated reference for the `qfa` package

```{toctree}
:hidden:
:maxdepth: 1

implementing-a-new-endpoint
```
