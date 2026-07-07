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
| `LLM_TIMEOUT_SECONDS` | no | `115.0` | Per-*attempt* LLM-call timeout. A single call retries transient failures (timeout, rate-limit) up to `3×` this budget; the orchestrator sizes the per-attempt timeout against the request deadline so the worst-case retry sequence still fits. |
| `LLM_MAX_TOTAL_TOKENS` | no | `100000` | Token budget guard. Estimated as `len(text) / LLM_CHARS_PER_TOKEN`. |
| `LLM_CHARS_PER_TOKEN` | no | `4` | Conversion ratio used by the token budget guard. |

## Embedding (`EMBEDDING_*`)

Only consumed by `mode=hierarchical`. The path defaults to *empty* at the
settings layer, but **the official Docker image bakes the default ONNX embedder
in (multilingual-e5-base) and sets the `ENV` to it** (see the builder stage in
`Dockerfile`), so a deployed image serves hierarchical out of the box. The 502
`analysis_unavailable` response only applies where the model is genuinely
absent — a bare local run, or a deployment that strips these vars. Running a
different family (e.g. BGE-M3) in production means adding a fetch step to the
`Dockerfile` model stage and overriding the `EMBEDDING_*` env — it is **not**
baked in by default.

Two model **families** are supported, selected by `EMBEDDING_MODEL_KIND`; the
family fixes the adapter's output handling (pooling + query prefix) while the
dimension and token cap are per-artifact knobs (`EMBEDDING_DENSE_DIM` /
`EMBEDDING_MAX_TOKENS`):

- **`e5`** (default, multilingual-e5-base, 768-d) — mean-pools token vectors
  and prepends the `query: ` prefix. Smaller and faster than BGE-M3 for a
  modest cross-lingual quality trade. e5-small (384-d) is the same `kind`.
- **`bge-m3`** (1024-d) — takes the model's already-pooled `dense_vecs` head
  as-is. The strongest cross-lingual model; select it when quality matters
  more than latency. Not baked into the image: add a fetch step to the
  `Dockerfile` model stage (`fetch_embedding_model.py --model bge-m3`) and
  point the `EMBEDDING_*` env at it (see the `Dockerfile` comment).

For **local development**, fetch an artifact and get the matching env lines
with `uv run python scripts/fetch_embedding_model.py` (defaults to e5-base;
`--model bge-m3` / `--model e5-small` for the others). It downloads to a
gitignored `.models/` and prints every `EMBEDDING_*` value to paste, including
`EMBEDDING_MODEL_KIND` and `EMBEDDING_DENSE_DIM`.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `EMBEDDING_MODEL_PATH` | for hierarchical | `""` (baked: `/app/models/multilingual-e5-base/onnx/model_qint8_avx512_vnni.onnx`) | Path to the mirrored ONNX graph. Never a HuggingFace URL in production. |
| `EMBEDDING_TOKENIZER_PATH` | for hierarchical | `""` (baked: `…/multilingual-e5-base/tokenizer.json`) | Path to the mirrored tokenizer file. Defaults to `EMBEDDING_MODEL_PATH` when empty. |
| `EMBEDDING_REVISION_HASH` | for hierarchical | `""` (baked: the pinned commit) | Pinned artifact revision/content hash. |
| `EMBEDDING_MODEL_KIND` | no | `e5` | Model family: `e5` (mean-pool + `query: ` prefix) or `bge-m3` (pre-pooled). Selects how the ONNX output becomes a vector. |
| `EMBEDDING_DENSE_DIM` | no | `768` | Expected output dimensionality, validated per batch so a mismatched artifact/config fails loud. e5-base 768; e5-small 384; BGE-M3 1024. |
| `EMBEDDING_MAX_TOKENS` | no | family default | Tokenizer truncation cap. Unset → the family's natural context (8192 for `bge-m3`, 512 for `e5`). Set lower to bound the per-record (and, since padding is per-batch, per-batch) cost from long outliers. |
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
| `ANALYZE_CODING_TREND_CODE_FIELDS` | no | `["coding_level_1", "coding_level_2", "coding_level_3"]` | JSON list. Metadata keys holding coding labels (comma-separated strings). |
| `ANALYZE_DEFAULT_CODING_TREND_PERIOD` | no | `week` | Server-side default granularity for the coding-trend table (`day` / `week` / `month`). Overridable per-request via the `period` body field. |

## Auth (`AUTH_*`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AUTH_API_KEYS` | **yes** | — | JSON array of {py:class}`~qfa.domain.models.TenantApiKey` objects. See [API key management](auth-management.md) for the shape. |

## Database (`DB_*`)

Usage tracking is always on, so a database connection is mandatory: the app
fails to start unless either `DB_URL` or the host/user/name parts below are
provided (see {py:class}`~qfa.settings.DatabaseSettings`).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DB_URL` | only if host/user/name not split | `""` | Full asyncpg URL. Used when supplied; otherwise built from the next four. |
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
