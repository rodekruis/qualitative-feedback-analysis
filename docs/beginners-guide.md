# Beginners guide

This guide explains the codebase in plain language. It assumes you can
program, but it assumes nothing about this project or its architecture. For
the full technical picture, read the [Architecture overview](architecture/index.md)
after this page.

The service reads qualitative feedback records, sends them to an LLM (a large
language model that generates text), and returns the results.

## Run the project

Clone the repository and start it with these commands.

```bash
git clone git@github.com:rodekruis/qualitative-feedback-analysis.git
cd qualitative-feedback-analysis
cp .env.example .env
uv sync
uv run python -m qfa.main
```

The service now runs at `http://0.0.0.0:8000`. For the full setup, including
hooks and test tiers, read the [Developer guide](development/index.md).

## The folders that matter

The codebase has many folders. As a beginner, you only need these:

- `src/qfa/domain/`: the core data shapes and business rules. This code never
  imports the LLM library, the database library, or any other outside tool.
- `src/qfa/services/`: one file per feature. Each file holds that feature's
  logic.
- `src/qfa/adapters/`: the code that talks to the outside world, such as the
  LLM provider, the database, and the tool that removes personal data from
  text.
- `src/qfa/api/`: the web layer. It receives requests and sends back answers.
  An endpoint is a web address the app answers. Add a new endpoint here.
- `tests/`: the automated tests.
- `docs/`: this documentation site.

## Add a new endpoint

Follow these steps to add a new endpoint.

1. Add the data shapes for the request and the result in
   `src/qfa/domain/models.py`.
2. Write the feature logic as a new class in `src/qfa/services/`.
3. Add the web request and response shapes in `src/qfa/api/schemas.py`.
4. Add the route in `src/qfa/api/routes.py`.
5. Wire your new class into `src/qfa/api/composition.py` and
   `src/qfa/api/dependencies.py`. This lets the route use it.
6. Add a test in `tests/api/`.

A route looks like this:

```python
@router.post("/v1/classify", response_model=ApiClassifyResponse, tags=["Inference"])
async def classify(body: ApiClassifyRequest, ...) -> ApiClassifyResponse:
    ...
```

This page skips the details. Read
[Implementing a new endpoint](development/implementing-a-new-endpoint.md) for
the full walkthrough. It shows real code for every step.

## Change the model

The app reads the model name from one environment variable, `LLM_MODEL`.

Before you change this value, make sure that the new model is already
deployed on the Azure AI Foundry resource. The app cannot call a model that
is not deployed there, and this repository does not deploy models for you.

Change the value in your `.env` file. The app uses the new model the next
time it starts.

```
LLM_MODEL=azure/gpt-5.4
```

The prefix before the slash names the provider, such as `azure/`,
`azure_ai/`, or `openai/`. A library called LiteLLM reads this prefix and
routes the request to the matching provider.

If you switch to a model the app does not already know the price of, add a
price entry to `src/qfa/resources/model_prices.yaml`. If you do not need
per-call cost tracking for the new model, skip this file.

The full list of model-related environment variables is in the
[Settings reference](operations/settings-reference.md). It also covers the
separate judge model.

## Where to go next

- [Developer guide](development/index.md): the full setup, test tiers, and
  coding conventions.
- [Architecture overview](architecture/index.md): why the codebase is
  structured this way.
- [Implementing a new endpoint](development/implementing-a-new-endpoint.md):
  the full, detailed walkthrough.
- [Settings reference](operations/settings-reference.md): every environment
  variable the app reads.
- [Ubiquitous language](ubiquitous_language.md): the shared vocabulary. Read
  it before you name anything new.
