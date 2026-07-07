# Todo: Guard auto-deploy on release publish

- [ ] 1. Prep: move spec to `docs/specs/guard-auto-deploy-on-publish.md`; commit plan + spec move
  - Acceptance: `SPEC.md` gone from root; spec under `docs/specs/`; Open Questions marked resolved
  - Verify: `git status`, file exists at new path
  - Files: SPEC.md → docs/specs/…, tasks/plan.md, tasks/todo.md

- [ ] 2. Guard script + tests (TDD)
  - Acceptance: `is_latest(tag, all_tags)` covers all 8 spec scenarios; CLI exits 0 (true/false), 2 (error)
  - Verify: `uv run pytest tests/scripts/test_is_latest_release.py`
  - Files: .github/scripts/is_latest_release.py, tests/scripts/test_is_latest_release.py

- [ ] 3. Reusable guard workflow
  - Acceptance: `_is-latest-release.yaml` takes `release_tag`, outputs `is_greatest`+`latest_tag`, `contents: write`
  - Verify: `python -c "import yaml,sys; yaml.safe_load(open(...))"`
  - Files: .github/workflows/_is-latest-release.yaml

- [ ] 4. Gate auto-staging-on-publish
  - Acceptance: staging deploy runs only when `is_greatest == true`; skip emits warning + summary; green on skip
  - Verify: YAML parse; logic review vs spec
  - Files: .github/workflows/auto-staging-on-publish.yaml

- [ ] 5. Gate docs.yaml release deploy
  - Acceptance: release-triggered Pages deploy gated on guard; manual dispatch ungated; skip warning
  - Verify: YAML parse
  - Files: .github/workflows/docs.yaml

- [ ] 6. Cleanup stale comments
  - Acceptance: no references to non-existent `build-release-image.yaml`; timing described correctly
  - Verify: `grep -rn build-release-image .github/` returns nothing
  - Files: .github/workflows/_deploy-release.yaml, .github/workflows/promote-to-dev.yaml

- [ ] 7. Docs
  - Acceptance: ADR-016 added + indexed + toctree; release-flow.md reflects guard/skip + corrected reviewer claims
  - Verify: `make docs`
  - Files: docs/adr/016-*.md, docs/adr/index.md, docs/operations/release-flow.md

- [ ] 8. Final gate + PR
  - Acceptance: lint + test + docs pass; draft PR opened
  - Verify: `make lint && make test && make docs`; `gh pr view`
  - Files: (none)
