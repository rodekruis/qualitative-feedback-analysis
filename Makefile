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
	uv run --no-sources pytest tests

test-doctest:
	# Run doctests in src/. Tolerates exit code 5 (no tests collected) so that
	# `make test` passes on a fresh project before any doctests have been added.
	uv run --no-sources pytest --doctest-modules src; rc=$$?; [ $$rc -eq 5 ] && exit 0 || exit $$rc

build:
	uv build

ty:
	uv run ty check

ruff:
	uv run --no-sources ruff format --check --target-version py312 src tests
	uv run --no-sources ruff check --fix --exit-non-zero-on-fix src tests

type_check: ty

lint: ruff type_check

pre_commit:
	pre-commit run --all-files

format:
	# format all code
	uv run ruff format
