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
| `POST` | `/v1/analyze-bulk` | Bulk free-text analysis over submitted feedback records |
| `POST` | `/v1/summarize` | Per-record summaries with quality scores |
| `POST` | `/v1/summarize-bulk` | Single bulk summary with judge score |
| `POST` | `/v1/assign-codes` | Hierarchical code assignment |
| `GET` | `/v1/usage` | Aggregate stats for the caller's tenant |
| `GET` | `/v1/usage/all/by-tenant` | Cross-tenant stats, tenants top-level with per-operation nested (requires `is_superuser=true`) |
| `GET` | `/v1/usage/all/by-operation` | Cross-tenant stats, operations top-level with per-tenant nested (requires `is_superuser=true`) |
| `GET` | `/v1/health` | Liveness probe; no auth |

## POST /v1/analyze-bulk — field reference

### Request

| Field | Type | Default | Description |
|---|---|---|---|
| `feedback_records` | list | — | Non-empty list of `{id, content, metadata?}` records. |
| `prompt` | string | — | Analyst question (1–4000 chars). |
| `output_language` | string or null | `null` | Free-text target language for the analysis output (e.g. `"Dutch"`, `"Brazilian Portuguese"`, `"Chinese (Simplified)"`) — any language the model can produce; not restricted to a fixed list. The value is sanitized (strip-and-keep: collapses whitespace, keeps only letters/spaces/hyphens/parentheses/apostrophes, caps at 50 chars) and never rejected. Prefer an ISO 639-1 code (`"nl"`) or English language name (`"Dutch"`) for the most predictable results. Omit (or `null`) to let the model answer in the language of the input records. Note: this is distinct from the fixed seven-language localization of the `pretty_output` header labels. |
| `anonymize` | bool | `true` | Anonymize record text before the LLM call. |
| `mode` | `"single_pass"` \| `"hierarchical"` | `"single_pass"` | `single_pass` runs one LLM call under the token cap (input over the cap → 413). `hierarchical` runs embed → cluster → map → reduce over large corpora and additionally returns `confidence`. |
| `period` | `"day"` \| `"week"` \| `"month"` \| null | `null` → server default (`week`) | Granularity for the deterministic `coding_trends` table. `day` for short-window deep-dives, `week` for the typical 1-3 month operational corpus, `month` for multi-year corpora. Omit to use the server-side default (`ANALYZE_DEFAULT_CODING_TREND_PERIOD`). |

### Response (200 OK)

| Field | Type | Notes |
|---|---|---|
| `analysis` | string | Model output with a server-side disclaimer prepended. |
| `quality_score` | float or null | Judge score in [0, 1]. `null` when the judge call failed (not an error — see `uncertainty_explanation`). |
| `uncertainty_explanation` | string | Natural-language judge reasoning, or a constant unavailable message when the judge failed. |
| `feedback_record_count` | int | Number of records submitted. |
| `request_id` | string | Canonical UUID matching the `X-Request-ID` response header. |
| `used_anonymization` | bool | Whether anonymization was applied. |
| `confidence` | float or null | Coverage-weighted mean of per-chunk faithfulness scores. Populated only for `mode=hierarchical`; `null` for `single_pass`. |
| `coding_trends` | object or null | Deterministic code-by-period frequency table. Populated for **both** modes whenever the configured date + code metadata fields are present (it depends only on metadata, not on the analysis pipeline). `null` when no record carries a parseable date. Bucket-label shape depends on `period`: `YYYY-MM-DD` for day, `YYYY-Www` (ISO week) for week, `YYYY-MM` for month. |

For `mode: "hierarchical"`, the response additionally populates `confidence`
(a coverage-weighted mean of per-chunk faithfulness). `coding_trends` is
populated for both modes, so existing single-pass integrations that ignored
the field are unaffected; clients that want trends can now read them from
the single-pass response too.

Per-record inference endpoints (`/v1/summarize`, `/v1/assign-codes`, `/v1/detect-sensitive`) accept a single `feedback_record` and return one result object, unlike bulk endpoints that accept multiple records and return aggregated output.

## Usage endpoint response shape

All usage endpoints return aggregated stats in two parallel views:

- **Per REST API call** (top-level fields): each distinct call to one of the analysis endpoints (`/v1/analyze-bulk`, `/v1/summarize`, `/v1/summarize-bulk`, `/v1/assign-codes`) counts as one. An endpoint like `/v1/assign-codes` that fans out to several LLM calls internally still shows up as a single entry here.
- **Per LLM call** (`llm_call_stats`): each individual LLM provider call counts as one. Use this view when you want to see raw provider traffic — for example to compute the LLM-calls-per-API-call ratio (`llm_call_stats.total_calls / total_calls`).

`GET /v1/usage` and `GET /v1/usage/all/by-tenant` carry an `operations` breakdown under each tenant block, sorted by `total_cost_usd` desc (ties: operation asc), with empty operations omitted. `GET /v1/usage/all/by-operation` flips the hierarchy: operations are top-level, each carrying a nested `tenants` breakdown (sorted by `total_cost_usd` desc, ties broken by `tenant_id` asc). Every block — at any level — carries its own `llm_call_stats`.

`total_cost_usd` sums every row in the window — including failed attempts that incurred a real cost — so the figure reflects what was actually spent. Distributions (`avg`/`min`/`max`/`p5`/`p95`) and token totals are computed over successful rows only so failures cannot skew them.

Full per-field semantics (including how `failed_calls` is counted for multi-LLM-call invocations and the `asyncio.gather` fan-out caveat on `call_duration`) live in the OpenAPI docs at `GET /docs`.

## curl examples

A minimal `analyze-bulk` call:

```bash
curl -X POST http://localhost:8000/v1/analyze-bulk \
  -H "Authorization: Bearer $LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "feedback_records": [
      {"id": "r-1", "content": "The coordination was good but shelter access was difficult."}
    ],
    "prompt": "Identify the top themes.",
    "mode": "single_pass"
  }'
```

Example 200 response:

```json
{
  "analysis": "Disclaimer: Generated by AI. Human review required.\n\nThe feedback highlights ...",
  "quality_score": 0.82,
  "uncertainty_explanation": "Coverage is high; all themes supported by at least two records.",
  "feedback_record_count": 1,
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
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
