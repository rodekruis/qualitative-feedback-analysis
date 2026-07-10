# Documentation

A short map of where things live. Each section links to its own index page with the full list.

The rendered version of this site is hosted on [GitHub Pages](https://rodekruis.github.io/qualitative-feedback-analysis/) and is refreshed when a release is published — see [Documentation publishing](operations/release-flow.md#documentation-publishing) for the trigger details.

## Language

- [Ubiquitous language](ubiquitous_language.md) — the domain vocabulary the API and code share. Read this before naming anything new.

## Develop

- [Developer guide](development/index.md) — getting from a fresh clone to a green `make test`. Covers local env setup (direnv + `.env`), pre-commit hooks, and the coding conventions we expect on PRs.
- [Implementing a new endpoint](development/implementing-a-new-endpoint.md) — a how-to for adding an HTTP endpoint end to end: domain and API models, the orchestrator use case, authentication, and usage tracking.
- [Devcontainer](https://github.com/mariushelf/copier-devcontainer) — an optional, per-developer secure Claude Code sandbox (uv, pre-commit, and a default-deny egress firewall). It is not shipped in this repo; inject it locally with `uvx copier copy gh:mariushelf/copier-devcontainer "$(pwd)"`.

## Architecture

- [Architecture overview](architecture/index.md) — how the service is structured (hexagonal layout, ports & adapters) and why. Start here if you're trying to understand the codebase.
- [Architecture decision records](adr/index.md) — the *why* behind individual design choices, kept in chronological order. Sits under Architecture in the Sphinx site.

## Operations

- [Operations](operations/index.md) — running, deploying, and observing the service. Covers infrastructure bootstrap, per-environment setup, API key management, settings reference, and observability.

## APIs & integrations

- [REST API overview](rest-api/index.md) — HTTP endpoint reference, the error envelope, and a pointer to the live OpenAPI docs served from a running instance.
- [Python API reference](python-api/index.md) — auto-generated reference for the `qfa` Python package (only rendered in the built Sphinx site).
- [EspoCRM connector scripts](integrations/espo-crm.md) — what the EspoCRM server-side scripts call and how they authenticate.
- [Migration guide for 0.14.0](migration/0.14.0-breaking-changes.md) — breaking field renames in 0.14.0.
- [GitHub releases](https://github.com/rodekruis/qualitative-feedback-analysis/releases) — the source of truth for what shipped in each version.
