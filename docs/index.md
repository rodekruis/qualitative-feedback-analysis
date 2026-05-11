# Qualitative Feedback Analysis

A backend that receives qualitative feedback records from a CRM, runs LLM-driven analysis, summarisation, and code assignment over them, and returns the results synchronously. Built as a FastAPI service on Azure App Service with a hexagonal core (LiteLLM, Presidio, Postgres usage tracking behind ports).

```{toctree}
:maxdepth: 1
:caption: Language

ubiquitous_language
```

```{toctree}
:maxdepth: 1
:caption: Develop

development/index
```

```{toctree}
:maxdepth: 1
:caption: Architecture

architecture/index
```

```{toctree}
:maxdepth: 1
:caption: Operations

operations/index
```

```{toctree}
:maxdepth: 1
:caption: APIs & integrations

rest-api/index
python-api/index
integrations/espo-crm
migration/0.14.0-breaking-changes
```
