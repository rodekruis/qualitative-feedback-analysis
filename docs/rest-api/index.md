# REST API

The HTTP API the backend exposes. For the auto-generated reference of the `qfa` Python package, see [Python API reference](../python-api/index.md) instead.

The live, always-current OpenAPI reference is served by FastAPI itself:

- **Swagger UI** — `GET /docs` on a running instance
- **OpenAPI JSON** — `GET /openapi.json` on a running instance

For local dev, that's `http://localhost:8000/docs`.

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

A minimal `analyze` call:

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H "Authorization: Bearer $LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "feedback_records": [
      {"id": "r-1", "content": "The coordination was good but shelter access was difficult."}
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
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "fields": [
      {"field": "feedback_records[0].content", "issue": "..."}
    ]
  }
}
```

`fields` only appears on 422. `request_id` is always present and matches the `X-Request-ID` response header. It is a canonical UUID string and is also the value persisted in the `llm_calls.call_id` column for every LLM call the request makes — quote the `request_id` when reporting an issue and ops can join logs and DB rows on it directly. See [Cross-cutting concerns § Error → HTTP mapping](../architecture/04-crosscutting.md) for the full mapping.

## Breaking changes

API field names changed in 0.14.0 (the ubiquitous-language migration). See the [migration guide for 0.14.0](../migration/0.14.0-breaking-changes.md).
