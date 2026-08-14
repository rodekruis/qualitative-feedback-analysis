# Bundled resources and test fixtures

Non-Python data files fall into two categories in this repo, and they are
stored and loaded differently. Picking the wrong one is the bug behind
[#158](https://github.com/rodekruis/qualitative-feedback-analysis/issues/158):
a data file the API needed at runtime lived outside the package and was reached
by walking up from `__file__`, so it vanished the moment `qfa` was installed
from a wheel.

| | Runtime resource | Test-only fixture |
|---|---|---|
| Lives in | `src/qfa/resources/` | `fixtures/` (repo root) |
| Loaded with | `importlib.resources` | `pathlib.Path` relative to the test file |
| Ships in the wheel | Yes | No |
| Read by | production code (`qfa.*`) | tests and `scripts/` helpers only |
| Examples | `model_prices.yaml`, `coding_framework.json` | `analyze_corpus.yaml` (2.9 MB), `large_corpus.yaml` |

## Runtime resources

Anything production code reads while serving a request — or while building the
app at startup — belongs inside the `qfa` package as a package resource, and
must be addressed through `importlib.resources`:

```python
import importlib.resources

resource = importlib.resources.files("qfa.resources").joinpath("model_prices.yaml")
data = resource.read_text(encoding="utf-8")
```

If you need a real filesystem path (to hand to a library that only takes
`str`/`Path`), wrap the read in `importlib.resources.as_file` and stay inside
the context manager:

```python
with importlib.resources.as_file(resource) as path:
    custom_prices = yaml.safe_load(path.read_text())
```

Never do this:

```python
# WRONG — assumes a repo checkout; lands in site-packages after `pip install`.
root = Path(__file__).resolve().parents[3]
data = (root / "fixtures" / "coding_framework.json").read_text()
```

`parents[...]` arithmetic encodes the *source* layout, not the *installed*
layout. It works from a checkout and from an editable install, then silently
breaks in the container image — silently, because such loaders usually have an
`if not path.is_file()` fallback, so the failure surfaces as degraded output
rather than an error. `qfa.api.schemas._assign_codes_request_examples` did
exactly that: the OpenAPI schema quietly served a single placeholder
"no-framework" example instead of the real coding framework, so Swagger's
"Try it out" was unusable in every deployed environment.

Current runtime resources, both in `src/qfa/resources/`:

- `model_prices.yaml` — custom LiteLLM cost entries, registered by
  {py:func}`~qfa.api.composition.register_custom_model_prices`.
- `coding_framework.json` — the COVID-19 coding framework used to build the
  `POST /v1/assign-codes` Swagger examples.

### Packaging

`[tool.hatch.build]` in `pyproject.toml` sets `packages = ["src/qfa"]`, and
hatchling ships **every tracked file** under that tree — `.py` or not — so a
new file dropped into `src/qfa/resources/` needs no `package-data` entry. Do
verify it after adding one, because a missing resource degrades quietly:

```bash
uv build --wheel
unzip -l dist/*.whl | grep qfa/resources
```

Both the wheel and the sdist are checked; `fixtures/` appears in neither.

## Test-only fixtures

Benchmark corpora stay in the repo-root `fixtures/` directory and are loaded by
path from the test that uses them:

```python
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CORPUS_PATH = FIXTURES / "analyze_corpus.yaml"
```

This is fine — and preferred — for test data: tests always run from a checkout,
and `analyze_corpus.yaml` alone is 2.9 MB, which has no business in an image
shipped to production. The rule of thumb: **if only tests and `scripts/` read
it, keep it out of the package.** The moment production code needs a fixture,
move it into `src/qfa/resources/` and switch every reader —
tests and scripts included — to `importlib.resources`, so there is exactly one
copy of the file.

## Where to go next

- [Developer guide](index.md) — environment setup, test tiers, coding conventions
- [Components](../architecture/03-components.md) — the composition root that loads `model_prices.yaml`
