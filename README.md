[![CI](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/ci.yaml/badge.svg)](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/ci.yaml)
[![CodeQL](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/rodekruis/qualitative-feedback-analysis/actions/workflows/github-code-scanning/codeql)

# Qualitative Feedback Analysis

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

Deployed to Azure App Service via Terraform (infrastructure) and GitHub Actions (app code).

## First-time setup

Before the CI/CD pipeline can run, the Azure infrastructure and GitHub environments must be bootstrapped locally. See [infra/BOOTSTRAP.md](infra/BOOTSTRAP.md).

## CI/CD pipeline

Releasing the backend is a two-step process:

1. **Release** (`release.yaml`) — trigger manually from the Actions tab. Runs CI, bumps the version via conventional commits, and creates a **draft** GitHub Release.
2. **Deploy** (`publish.yaml`) — runs automatically when you publish the draft release. Pushes app settings and deploys the code to Azure.

**Infrastructure**:

infrastructure (Azure App Service and other resources) and github environments are
managed by terraform.

Terraform is applied via the `terraform.yaml`. It runs automatically when any commits
with changes to any file in the `infra` folder are pushed to the main branch.

## GitHub Configuration

GitHub environments (`dev`, `prd`) and their required Actions variables are managed by Terraform. See [infra/BOOTSTRAP.md](infra/BOOTSTRAP.md).


# Getting Started

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

```bash
uv sync
```

## Configuration

### Environment Variables

Create a `.env` file in the project root (or export the variables in your shell). Only two variables are required; all others have sensible defaults.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_API_KEY` | **yes** | — | API key for OpenAI or Azure OpenAI |
| `AUTH_API_KEYS` | **yes** | — | JSON array of API key objects (see below) |
| `LLM_PROVIDER` | no | `openai` | LLM backend: `openai` or `azure_openai` |
| `LLM_MODEL` | no | `gpt-4.1-mini` | Model name |
| `LLM_AZURE_ENDPOINT` | no | `""` | Azure OpenAI endpoint URL (required when provider is `azure_openai`) |
| `LLM_API_VERSION` | no | `""` | Azure OpenAI API version (required when provider is `azure_openai`) |
| `LLM_TIMEOUT_SECONDS` | no | `115.0` | Timeout for LLM calls in seconds |
| `LLM_MAX_RETRIES` | no | `3` | Max retry attempts for LLM calls |
| `LLM_MAX_TOTAL_TOKENS` | no | `100000` | Token budget for entire request |
| `ORCHESTRATOR_METADATA_FIELDS_TO_INCLUDE` | no | `[]` | Metadata fields forwarded to the LLM |
| `ORCHESTRATOR_RETRY_BASE_SECONDS` | no | `1.0` | Initial backoff delay |
| `ORCHESTRATOR_RETRY_MULTIPLIER` | no | `2.0` | Exponential backoff multiplier |
| `ORCHESTRATOR_RETRY_JITTER_FACTOR` | no | `0.5` | Jitter factor for backoff |
| `ORCHESTRATOR_RETRY_CAP_SECONDS` | no | `10.0` | Maximum backoff delay |
| `ORCHESTRATOR_CHARS_PER_TOKEN` | no | `4` | Chars-per-token estimate ratio |

Minimal `.env` example:

```dotenv
LLM_API_KEY=sk-your-openai-key
AUTH_API_KEYS='[{"name":"crm-production","key":"sk-prod-abc123def456","tenant_id":"tenant-redcross-nl"}]'
```

### API Keys

API authentication is configured via the `AUTH_API_KEYS` environment variable. The value is a JSON array of objects, each representing a tenant with its own API key.

**Format** — a JSON array of objects, each with three fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable label for the key (e.g. `"crm-production"`) |
| `key` | string | The secret API key value |
| `tenant_id` | string | Tenant identifier associated with this key |

**Example:**

```bash
export AUTH_API_KEYS='[
    {"name": "crm-production", "key": "sk-prod-abc123def456", "tenant_id": "tenant-redcross-nl"},
    {"name": "staging", "key": "sk-staging-xyz789", "tenant_id": "tenant-staging"}
]'
```

**How it works:**

1. At startup the application parses the JSON and validates every entry.
2. Clients authenticate by sending an `Authorization: Bearer <key>` header.
3. The key is matched using constant-time comparison (`secrets.compare_digest`) to prevent timing attacks.
4. On success, the request is tagged with the matching `tenant_id`.

## Running the Application

```bash
uv run python -m qfa.main
```

The server starts on `http://0.0.0.0:8000`. For development with auto-reload:

```bash
uv run uvicorn qfa.main:app --reload --host 0.0.0.0 --port 8000
```

# Development

## Setup

```bash
uv sync
uv run pre-commit install
```

## Running Tests

```bash
make test
```

## Linting

```bash
make lint
```

## Formatting

```bash
make format
```
