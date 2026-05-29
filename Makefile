SHELL := /bin/bash

.PHONY: sync clean test test-unit-tests test-doctest build ty ruff type_check lint pre_commit format

sync:
	uv sync --all-extras

clean:
	rm -rf dist
	rm -rf .artifacts
	rm -rf .mypy_cache
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +


test: test-unit-tests test-doctest

test-unit-tests:
	uv run pytest tests

test-doctest:
	# Run doctests in src/. Tolerates exit code 5 (no tests collected) so that
	# `make test` passes on a fresh project before any doctests have been added.
	uv run pytest --doctest-modules src; rc=$$?; [ $$rc -eq 5 ] && exit 0 || exit $$rc

build:
	uv build

ty:
	uv run ty check

ruff:
	uv run ruff format --target-version py312 src tests
	uv run ruff check --fix --exit-non-zero-on-fix src tests

type_check: ty

lint_imports:
	uv run lint-imports

lint: ruff type_check lint_imports

pre_commit:
	pre-commit run --all-files

format:
	# format all code
	uv run ruff format

.PHONY: db-up db-down db-reset migrate test-integration

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d postgres

migrate:
	uv run python -m qfa.cli.migrate

test-integration:
	uv run pytest -m "integration or e2e"

.PHONY: docs docs-clean docs-live

# Build the HTML documentation locally. Combines the auto-generated API
# reference (sphinx-autosummary against src/qfa) with the prose pages
# under docs/. Output lands at docs/_build/html/index.html.
#
# Strict mode (-W) fails the build on any warning so broken cross-refs,
# orphaned docs, or autodoc surprises are caught at build time. Combined
# with --keep-going so a single build surfaces every issue at once.
docs:
	uv sync --group docs
	uv run sphinx-build -W --keep-going -a -b html docs docs/_build/html
	@echo "Open docs/_build/html/index.html"

# Wipe generated docs artefacts. Useful when autosummary stubs go stale
# after renames or when warnings stop reproducing.
docs-clean:
	rm -rf docs/_build docs/python-api/_apidoc

# Live-reload mode: rebuilds on file change. Handy while writing docs.
docs-live:
	uv sync --group docs
	uv run sphinx-autobuild -a -b html docs docs/_build/html --watch src/qfa --open-browser
