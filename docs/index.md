# Qualitative Feedback Analysis

A backend that receives qualitative feedback records from a CRM, runs LLM-driven analysis, summarisation, and code assignment over them, and returns the results synchronously. Built as a FastAPI service on Azure App Service with a hexagonal core (LiteLLM, Presidio, Postgres usage tracking behind ports).

Use the sidebar to navigate by audience, or jump straight to the [auto-generated Python API reference](python-api/index.md).

```{toctree}
:hidden:
:caption: For contributors

development/index
```

```{toctree}
:hidden:
:caption: For developers

architecture/index
adr/index
ubiquitous_language
```

```{toctree}
:hidden:
:caption: For operators

operations/index
```

```{toctree}
:hidden:
:caption: APIs

rest-api/index
python-api/index
```

```{toctree}
:hidden:
:caption: For integrators

integrations/espo-crm
```

```{toctree}
:hidden:
:caption: Release notes

migration/0.14.0-breaking-changes
```
