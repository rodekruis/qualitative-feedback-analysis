# Pinned revision of the self-hosted ONNX embedder (ADR-014). The default
# model, multilingual-e5-base, is baked into the image so embeddings work on
# every deploy with no runtime HuggingFace dependency. The SHA resolves a
# moving HF `main` ref to an immutable commit. To bump it: pick a newer commit,
# re-run the cosine-validation e2e test, then update here AND in
# scripts/fetch_embedding_model.py (the MODELS registry) in lockstep.
# Declared before the first FROM so both stages inherit it; changing it busts
# the model-fetch layer cache and its runtime ENV together.
#
# Only e5-base is baked. To run a different family (e.g. BGE-M3) fetch it into
# the image — add a fetch step below using scripts/fetch_embedding_model.py
# --model bge-m3 — and point the EMBEDDING_* env at it.
ARG EMBEDDING_E5_REVISION=d128750597153bb5987e10b1c3493a34e5a4502a

# ── Stage: fetch the embedding models ────────────────────────────────────
# INTERIM: this fetches from HuggingFace at *build* time, pinned by revision.
# ADR-014 ultimately wants the artifacts mirrored to an artifact store we
# control (e.g. an Azure Blob container) and pulled from there. When that
# mirror exists, replace the fetches below with a COPY/download from it — the
# rest of the image is unaffected.
FROM python:3.13-slim AS model
ARG EMBEDDING_E5_REVISION
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /build
COPY scripts/fetch_embedding_model.py .
# --no-project: don't try to resolve the repo's pyproject; --with pulls just
# the one dependency the script needs into an ephemeral environment.
RUN uv run --no-project --with "huggingface-hub>=1.10" \
        python fetch_embedding_model.py --model e5-base \
        --revision "${EMBEDDING_E5_REVISION}" \
        --dest /models/multilingual-e5-base

# ── Runtime image ────────────────────────────────────────────────────────
FROM python:3.13-slim
ARG EMBEDDING_E5_REVISION
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Model layer first: it's large and rarely changes, so keeping it above the
# frequently-churning app code means it stays cached (and isn't re-pushed)
# until the pinned revision changes. Only the default model (e5-base) is
# baked; the ENV below points at it. To run a different family, bake it in
# (add a fetch step in the model stage above) and override the EMBEDDING_*
# env accordingly.
COPY --from=model /models /app/models
ENV EMBEDDING_MODEL_PATH=/app/models/multilingual-e5-base/onnx/model_qint8_avx512_vnni.onnx \
    EMBEDDING_TOKENIZER_PATH=/app/models/multilingual-e5-base/tokenizer.json \
    EMBEDDING_REVISION_HASH=${EMBEDDING_E5_REVISION} \
    EMBEDDING_MODEL_KIND=e5 \
    EMBEDDING_DENSE_DIM=768 \
    EMBEDDING_MAX_TOKENS=512

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-editable --no-dev --frozen
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && uv sync --no-editable --no-dev --frozen
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
