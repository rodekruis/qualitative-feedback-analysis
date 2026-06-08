# Pinned revision of the self-hosted BGE-M3 ONNX-int8 embedder (ADR-014).
# This is the HuggingFace repo gpahal/bge-m3-onnx-int8 at the tip of `main`
# as of 2026-05-29 (resolved to its commit SHA so the pin is immutable —
# `main` is a moving ref). To bump: pick a newer commit, re-run the
# cosine-validation e2e test against official BAAI/bge-m3, then update here.
# Declared before the first FROM so both stages inherit it; changing it busts
# the model-fetch layer cache and the runtime ENV in lockstep.
ARG EMBEDDING_REVISION=2b34e84df040034d4b9eabb62383a87c18955822

# ── Stage: fetch the embedding model ─────────────────────────────────────
# Baked into the image so embeddings work on every deploy with no runtime
# HuggingFace dependency (ADR-014: never fetch from HF at runtime).
#
# INTERIM: this fetches from HuggingFace at *build* time, pinned by revision.
# ADR-014 ultimately wants the artifact mirrored to an artifact store we
# control (e.g. an Azure Blob container) and pulled from there. When that
# mirror exists, replace the fetch below with a COPY/download from it — the
# rest of the image is unaffected.
FROM python:3.13-slim AS model
ARG EMBEDDING_REVISION
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /build
COPY scripts/fetch_embedding_model.py .
# --no-project: don't try to resolve the repo's pyproject; --with pulls just
# the one dependency the script needs into an ephemeral environment.
RUN uv run --no-project --with "huggingface-hub>=1.10" \
        python fetch_embedding_model.py \
        --revision "${EMBEDDING_REVISION}" \
        --dest /models/bge-m3-onnx-int8

# ── Runtime image ────────────────────────────────────────────────────────
FROM python:3.13-slim
ARG EMBEDDING_REVISION
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Model layer first: it's large (~570 MB) and rarely changes, so keeping it
# above the frequently-churning app code means it stays cached (and isn't
# re-pushed) until the pinned revision changes.
COPY --from=model /models /app/models
ENV EMBEDDING_MODEL_PATH=/app/models/bge-m3-onnx-int8/model_quantized.onnx \
    EMBEDDING_TOKENIZER_PATH=/app/models/bge-m3-onnx-int8/tokenizer.json \
    EMBEDDING_REVISION_HASH=${EMBEDDING_REVISION}

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-editable --no-dev --frozen
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && uv sync --no-editable --no-dev --frozen
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
