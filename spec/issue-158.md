## Summary

Fixtures and bundled data files are loaded by **constructing filesystem paths relative to the module location** (e.g. `Path(__file__).resolve().parents[3] / "fixtures" / ...`) instead of addressing them as package resources via `importlib.resources`. This is fragile: it assumes the repository directory layout and silently breaks once `qfa` is installed as a (non-editable) wheel, where the repo-root `fixtures/` directory does not exist relative to the installed module.

The canonical example is the coding-framework fixture used to build Swagger examples in `src/qfa/api/schemas.py`.

## Where it happens

**Production code (highest priority — runs at app startup, feeds OpenAPI/Swagger examples):**

- `src/qfa/api/schemas.py:92-124` — `_assign_codes_request_examples()` does:
  ```python
  root = Path(__file__).resolve().parents[3]
  path = root / "fixtures" / "coding_framework.json"
  if not path.is_file():
      return [ ... "no-framework" fallback ... ]
  framework = json.loads(path.read_text(encoding="utf-8"))
  ```
  `parents[3]` walks up out of the installed package to guess the repo root. After a real `pip install` (non-editable) this path lands in `site-packages` and the file is absent, so the endpoint silently degrades to the placeholder "no-framework" example.

**Test code (lower priority — see scope note below):**

- `tests/test_analyze_corpus.py:20-22,45,54` — `Path(__file__).resolve().parent.parent / "fixtures"`
- `tests/test_large_corpus.py:18-19,29` — same pattern
- `tests/scripts/test_stress_analyze.py:23,31,43` — `Path(__file__).resolve().parents[2] / "fixtures" / ...`

## Existing good pattern to follow

`src/qfa/api/composition.py:93-97` already loads bundled data the correct way:

```python
prices_path = importlib.resources.files("qfa.resources").joinpath("model_prices.yaml")
with importlib.resources.as_file(prices_path) as f:
    custom_prices = yaml.safe_load(f.read_text())
```

The fix should make the rest of the codebase consistent with this.

## Proposed change

1. Move data that **production code** needs at runtime into a package resource directory (e.g. `src/qfa/resources/coding_framework.json`, alongside `model_prices.yaml`) and load it via `importlib.resources.files("qfa.resources")`.
2. Ensure `pyproject.toml` packages/ships these resources. Currently `[tool.hatch.build]` only sets `packages = ["src/qfa"]` with no package-data / include for non-`.py` files — confirm `.json`/`.yaml` resources are included in the wheel.
3. Decide on test-only fixtures (see scope note).

## Scope note: runtime data vs. test-only data

Distinguish two cases — they should **not** be solved the same way:

- **Runtime data** (e.g. `coding_framework.json` used by `schemas.py`): belongs inside the package as an `importlib.resources` resource so it ships in the wheel.
- **Test-only fixtures** (e.g. the 2.9 MB `fixtures/analyze_corpus.yaml`, `large_corpus.yaml`): should **not** be bloating the shipped wheel. These can either stay as path-based loads in tests (tests always run from a checkout) or move under a dedicated test package. Avoid blindly bundling large test corpora into the distribution.

## Acceptance criteria

- [ ] `src/qfa/api/schemas.py` loads the coding-framework example via `importlib.resources`, not `Path(__file__).parents[...]`.
- [ ] Swagger "Try it out" examples work from a non-editable install (no silent "no-framework" fallback when the resource is bundled).
- [ ] `pyproject.toml` is configured so bundled resources are included in the build, and this is verified (e.g. inspect the built wheel).
- [ ] Test-only fixtures handling is explicitly decided (kept path-based or moved), with large corpora kept out of the shipped wheel.
- [ ] Docs under `docs/` updated if resource loading / packaging is documented there.
