# Documentation

A short map of where things live. Each section links to its own index page with the full list.

## For contributors

- [Developer guide](development/index.md) — getting from a fresh clone to a green `make test`. Covers local env setup (direnv + `.env`), pre-commit hooks, and the coding conventions we expect on PRs.
- [Devcontainer](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/.devcontainer/README.md) — the pre-built dev environment with uv, pre-commit, Claude Code, and a default-deny egress firewall. The fastest path if you don't want to install toolchains on your host.

## For developers

- [Architecture](architecture/index.md) — how the service is structured (hexagonal layout, ports & adapters) and why. Start here if you're trying to understand the codebase.
- [Architecture decision records](adr/index.md) — the *why* behind individual design choices, kept in chronological order.
- [Ubiquitous language](ubiquitous_language.md) — the domain vocabulary the API and code share. Read this before naming anything new.

## For operators

- [Operations](operations/index.md) — running, deploying, and observing the service. Covers infrastructure bootstrap, per-environment setup, API key management, settings reference, and observability.

## APIs

- [REST API overview](rest-api/index.md) — HTTP endpoint reference, the error envelope, and a pointer to the live OpenAPI docs served from a running instance.
- [Python API reference](python-api/index.md) — auto-generated reference for the `qfa` Python package (only rendered in the built Sphinx site).

## For integrators

- [EspoCRM connector scripts](integrations/espo-crm.md) — what the EspoCRM server-side scripts call and how they authenticate.

## Release notes

- [GitHub releases](https://github.com/rodekruis/qualitative-feedback-analysis/releases) — the source of truth for what shipped in each version.
- [Migration guide for 0.14.0](migration/0.14.0-breaking-changes.md) — breaking field renames in 0.14.0.
