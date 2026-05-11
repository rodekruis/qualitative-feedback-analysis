# API reference

The live, always-current API reference is served by FastAPI itself:

- **Swagger UI** — `GET /docs` on a running instance
- **OpenAPI JSON** — `GET /openapi.json` on a running instance

For local dev, that's `http://localhost:8000/docs`.

> A hosted reference site (Sphinx-based, served from GitHub Pages) is planned. Until it ships, the running-instance `/docs` is the source of truth.

## Quick reference

All endpoints except `GET /v1/health` require `Authorization: Bearer <key>`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/analyze` | Free-text analysis of submitted feedback records |
| `POST` | `/v1/summarize` | Per-record summaries with quality scores |
| `POST` | `/v1/summarize-aggregate` | Single aggregate summary with judge score |
| `POST` | `/v1/assign_codes` | Hierarchical code assignment |
| `GET` | `/v1/usage` | Aggregate stats for the caller's tenant |
| `GET` | `/v1/usage/all` | Cross-tenant stats (requires `is_superuser=true`) |
| `GET` | `/v1/health` | Liveness probe; no auth |

## curl examples

The repo ships a [`local-testing.http`](../../local-testing.http) file with minimal example calls for every endpoint. Open it in any editor that supports REST clients (VS Code REST Client, JetBrains HTTP Client, etc.), or read it and translate to curl.

A minimal `analyze` call:

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H "Authorization: Bearer $LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "feedback_records": [
      {"id": "r-1", "text": "The coordination was good but shelter access was difficult."}
    ],
    "prompt": "Identify the top themes."
  }'
```

## Error envelope

Every error response shares this shape:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "request_id": "req_…",
    "fields": [
      {"field": "feedback_records[0].text", "issue": "..."}
    ]
  }
}
```

`fields` only appears on 422. `request_id` is always present and matches the `X-Request-ID` response header. See [04-crosscutting.md § Error → HTTP mapping](../architecture/04-crosscutting.md) for the full mapping.

## Breaking changes

API field names changed in 0.14.0 (the ubiquitous-language migration). See the [migration guide for 0.14.0](../migration/0.14.0-breaking-changes.md).
