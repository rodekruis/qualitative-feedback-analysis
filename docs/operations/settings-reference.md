# Settings reference

Every environment variable the app reads. Settings are loaded by `pydantic-settings` at startup; missing required variables cause the app to fail fast.

> **Tip:** rather than editing this table by hand, you can `uv run python -c "from qfa.settings import AppSettings; import json; print(AppSettings.model_json_schema())"` to dump the live schema.

## LLM (`LLM_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `LLM_MODEL` | no | `azure_ai/mistral-medium-2505` | Routed by LiteLLM based on the prefix (`azure/…`, `azure_ai/…`, `openai/…`, …). |
| `LLM_API_KEY` | **yes** | — | Provider API key. Stored as `SecretStr`. |
| `LLM_API_BASE` | only some providers | `""` | E.g. `https://<resource>.openai.azure.com/` for Azure OpenAI. |
| `LLM_API_VERSION` | only some providers | `""` | API version where the provider expects one. |
| `LLM_TIMEOUT_SECONDS` | no | `115.0` | Per-LLM-call timeout. |
| `LLM_MAX_TOTAL_TOKENS` | no | `100000` | Token budget guard. Estimated as `len(text) / LLM_CHARS_PER_TOKEN`. |
| `LLM_CHARS_PER_TOKEN` | no | `4` | Conversion ratio used by the token budget guard. |

## Embedding (`EMBEDDING_*`)

Only consumed by `mode=hierarchical`. The model defaults to *empty* at the
settings layer, but **the official Docker image bakes the BGE-M3 ONNX
artifact in and sets all three paths as `ENV`** (see the builder stage in
`Dockerfile`), so a deployed image serves hierarchical out of the box. The
502 `analysis_unavailable` response only applies where the model is genuinely
absent — a bare local run, or a deployment that strips these vars.

For **local development**, fetch the artifact and get the matching env lines
with `uv run python scripts/fetch_embedding_model.py` (downloads to a
gitignored `.models/` and prints the three `EMBEDDING_*` values to paste).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `EMBEDDING_MODEL_PATH` | for hierarchical | `""` (baked: `/app/models/bge-m3-onnx-int8/model_quantized.onnx`) | Path to the mirrored BGE-M3 `model_quantized.onnx`. Never a HuggingFace URL in production. |
| `EMBEDDING_TOKENIZER_PATH` | for hierarchical | `""` (baked: `…/tokenizer.json`) | Path to the mirrored tokenizer file. Defaults to `EMBEDDING_MODEL_PATH` when empty. |
| `EMBEDDING_REVISION_HASH` | for hierarchical | `""` (baked: the pinned commit) | Pinned artifact revision/content hash. |
| `EMBEDDING_INTRA_OP_NUM_THREADS` | no | core count | onnxruntime intra-op threads for the batched encode. |
| `EMBEDDING_BATCH_SIZE` | no | `100` | Records encoded per onnxruntime batch. The corpus is embedded in sequential batches of this size to bound peak memory on large inputs (padding is per-batch). Lower it if the embedder is memory-pressured; raise it for throughput on roomy hosts. |

## Orchestrator (`ORCHESTRATOR_*`)

Cross-cutting orchestrator wiring shared by every endpoint (retry
policy, token-budget estimation, metadata allow-list). Endpoint-specific
tuning lives in its own settings group (see *Analyze* below) so the
eventual per-endpoint orchestrator split (ADR-011) doesn't require
renaming environment variables in production.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ORCHESTRATOR_METADATA_FIELDS_TO_INCLUDE` | no | `[]` | JSON list. Metadata keys allowed to reach the LLM. |

## Analyze (`ANALYZE_*`)

Configuration specific to `POST /v1/analyze` (both `mode=single_pass`
and `mode=hierarchical`). The coding-trend knobs apply to both modes;
the clustering knobs are only consulted on the hierarchical path.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ANALYZE_MIN_CLUSTER_SIZE` | no | `5` | HDBSCAN `min_cluster_size` for the map-step chunking (`mode=hierarchical`). |
| `ANALYZE_CLUSTERING_METRIC` | no | `euclidean` | HDBSCAN distance metric (`mode=hierarchical`). |
| `ANALYZE_MAX_CONCURRENT_CHUNKS` | no | `8` | Max map-step chunks analysed concurrently (`mode=hierarchical`). Each chunk is one analysis call + one leaf-judge call; this bounds the fan-out so a large corpus doesn't burst past the provider's rate limit. `1` = fully sequential. |
| `ANALYZE_TARGET_CHUNK_TOKENS` | no | `4000` | Target chunk size in estimated tokens — the chunking *granularity* knob (`mode=hierarchical`), decoupled from the LLM hard cap. HDBSCAN clusters are uneven, so a dominant theme can fit the cap whole and become one fat, slow map call; a cluster over this target is split into roughly equal, date-ordered sub-chunks. Effective split budget is `min(this, LLM_MAX_TOTAL_TOKENS)`, so a chunk never overflows a call. Lower for more, smaller, more-parallel calls; raise for fewer, larger ones. |
| `ANALYZE_CODING_TREND_DATE_FIELD` | no | `created` | Metadata key holding the record date for the coding-trend table. |
| `ANALYZE_CODING_TREND_CODE_FIELDS` | no | `["codes"]` | JSON list. Metadata keys holding coding labels (comma-separated strings). |
| `ANALYZE_DEFAULT_CODING_TREND_PERIOD` | no | `week` | Server-side default granularity for the coding-trend table (`day` / `week` / `month`). Overridable per-request via the `period` body field. |

## Auth (`AUTH_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AUTH_API_KEYS` | **yes** | — | JSON array of {py:class}`~qfa.domain.models.TenantApiKey` objects. See [API key management](auth-management.md) for the shape. |

## Database (`DB_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DB_TRACK_USAGE` | no | `false` | Master switch for usage tracking. When `false`, none of the other `DB_*` variables are required. |
| `DB_URL` | only if `DB_TRACK_USAGE=true` and host/user not split | `""` | Full asyncpg URL. Used when supplied; otherwise built from the next four. |
| `DB_HOST` | only if `DB_URL` not set | `""` | |
| `DB_PORT` | no | `5432` | |
| `DB_NAME` | only if `DB_URL` not set | `""` | |
| `DB_USER` | only if `DB_URL` not set | `""` | For `entra` mode, the managed-identity principal name. |
| `DB_PASSWORD` | only when `DB_AUTH_MODE=password` | — | Stored as `SecretStr`. |
| `DB_AUTH_MODE` | no | `password` | `password` or `entra`. |
| `DB_AAD_SCOPE` | no | `https://ossrdbms-aad.database.windows.net/.default` | AAD scope for the access token (Entra mode only). |

## Logging (`LOG_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `LOG_LOGLEVEL` | no | `DEBUG` | Level for the `qfa` package (string or numeric). |
| `LOG_LOGLEVEL_3RDPARTY` | no | `WARNING` | Level for third-party libraries. |
