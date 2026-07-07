# Plan: Guard auto-deploy on release publish

Spec: `docs/specs/guard-auto-deploy-on-publish.md` (moved from repo-root `SPEC.md`).

## Approach

Extract the semver "is this the greatest release?" decision into a unit-tested
Python script under `.github/scripts/`, wrap it in one reusable workflow, and gate
**both** `release: published` auto-deploy paths (staging + docs Pages) on it. Manual
promotes stay version-unguarded (rollback). Fail closed on guard error.

## Decisions locked (from grilling + /build args)

- Spec moves to `docs/specs/`.
- Skip notice = `::warning::` annotation + `$GITHUB_STEP_SUMMARY` only (no release comment).
- Stale `build-release-image.yaml` comment cleanup is included in this PR.
- Tests live in `tests/scripts/` (existing repo convention), importing the script via `importlib`.
- Guard runs `uv run --no-project --with packaging` (no full project sync just to compare versions).

## Task order (each = RED→GREEN→regression→build→commit)

1. Prep: move spec to `docs/specs/`, commit plan artifacts.
2. Guard script + tests — the only genuinely testable unit (real TDD).
3. Reusable guard workflow `_is-latest-release.yaml`.
4. Gate `auto-staging-on-publish.yaml` + skip warning.
5. Gate `docs.yaml` release-deploy + skip warning (manual dispatch stays ungated).
6. Cleanup stale `build-release-image.yaml` comments.
7. Docs: ADR-016 + `docs/adr/index.md` + `docs/operations/release-flow.md`.
8. Final gate: `make lint` + `make test` + `make docs`; open draft PR.

## Verification note

Workflow YAML is not executed by any test harness in this repo. Its tasks are
verified by (a) `python -c yaml.safe_load` parse, (b) the script's pytest suite for
the decision logic, and (c) a documented manual dry-run before merge. The safety
logic lives in the script precisely so it *is* testable.
