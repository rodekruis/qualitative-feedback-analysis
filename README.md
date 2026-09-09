[![CI](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/ci.yaml/badge.svg)](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/ci.yaml)
[![CodeQL](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/github-code-scanning/codeql)

# Qualitative Feedback Analysis

A backend that receives qualitative feedback records from a CRM, runs LLM-driven analysis, summarisation, and code assignment over them, and returns the results synchronously. Each request carries dozens to thousands of records.

Built as a FastAPI service on Azure App Service, with a hexagonal core (LiteLLM, Presidio, Postgres usage tracking behind ports) and a Terraform-managed infrastructure.

## Documentation

The rendered Sphinx site is hosted at <https://rodekruis.github.io/qualitative-feedback-analysis/> (refreshed when a release is published — see [Documentation publishing](docs/operations/release-flow.md#documentation-publishing) for trigger details).

All long-form docs live in the [documentation home](docs/README.md). The most common entry points:

- **New to the project?** Start with the [Developer guide](docs/development/index.md) — local env setup, pre-commit, coding conventions.
- **Want a containerised dev environment?** The [devcontainer](https://github.com/mariushelf/copier-devcontainer) is an optional, per-developer sandbox. It is not shipped in this repo — inject it locally with `uvx copier copy gh:mariushelf/copier-devcontainer "$(pwd)"`.
- **Want to understand the design?** Read the [Architecture overview](docs/architecture/index.md).
- **Operating the service?** See the [Operations index](docs/operations/index.md) — deployment, release flow, env provisioning, observability, settings reference.
- **Calling the API?** See the [REST API overview](docs/rest-api/index.md).
- **Integrating from EspoCRM?** See the [EspoCRM connector scripts](docs/integrations/espo-crm.md).

## Quick start

```bash
git clone git@github.com:rodekruis/qualitative-feedback-analysis.git
cd qualitative-feedback-analysis
cp .env.example .env                    # boots as-is — see below before any LLM call
uv sync
uv run pre-commit install
make test
uv run python -m qfa.main               # serves on http://0.0.0.0:8000
```

Three variables are required — `LLM_API_KEY`, `AUTH_API_KEYS`, `DB_URL` — and the copied template satisfies all three, so the service starts and `GET /v1/health` answers 200 with no editing. Two placeholders need replacing before real use: `LLM_API_KEY` (LiteLLM only authenticates on the first call, so a dummy string boots) and, for any route that records usage, a reachable `DB_URL` — `docker compose up -d postgres` matches the shipped default.

Full walkthrough (direnv, hooks, conventions, test tiers) is in the [Developer guide](docs/development/index.md). Required and optional environment variables are listed in the [Settings reference](docs/operations/settings-reference.md).

## License

See [LICENSE](LICENSE).
