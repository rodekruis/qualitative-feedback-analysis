# Marius Helf's new project

[![Tests](https://github.com/mariushelf/feedback-analysis-backend/actions/workflows/cicd.yaml/badge.svg)](https://github.com/mariushelf/feedback-analysis-backend/actions/workflows/cicd.yaml)
[![PyPI version](https://badge.fury.io/py/feedback-analysis-backend.svg)](https://pypi.org/project/feedback-analysis-backend/)


# About

The feedback analysis tool receives feedback documents and analyses trends, topics and their evolution over time.

The feedback is collected in a CRM system and sent to this backend for analysis.

Each request contains dozens to thousands of feedback documents.

The documents need to be analysed and, and the result sent back to the CRM system in a
synchronous API call.

## Tech stack
* fastapi
* uvicorn
* pydantic for settings manangement and environment loading
* OpenAI API for document analysis

## Architecture
* hexagonal architecture.
* Flow: API call(documents) -> Orchestrator -> LLM API -> return result to user.
* The Orcehstrator is an exchangeable service. 
  * naive version: forward all documents to the LLM in one call, together with system prompt and user prompt
  * possible future versions: apply embedding, chunking, other "smart" techniques, possibly multiple LLM calls.

## Requirements
* only authenticated API calls (via API keys)
* synchronous API calls impose a limit on processing time (TBD, let's assume 2 minutes for now)

## Non-functional requirements:
* LLM provider must be exchangeable: declared via an `LLMPort`, so that implementation can be swapped
* Orchestrator must be swappable depending on task.
  TBD: either via different API end points, API request parameters or automatically depending on task
* hardened security.

# Deployment
* Azure cloud


# Installation

```bash
pip install feedback-analysis-backend
```


# Development

## Setup

```bash
uv sync
uv run pre-commit install
```

## Running tests

```bash
make test
```

## Linting

```bash
make lint
```

# Contact

Marius Helf ([marius@xomnia.com](mailto:marius@xomnia.com))
