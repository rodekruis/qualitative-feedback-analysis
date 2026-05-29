"""End-to-end validation of the mirrored BGE-M3 ONNX artifact.

Why: the spec requires a one-time check that the mirrored ONNX-int8 build
matches official ``BAAI/bge-m3`` (expect cosine ~0.999) to catch a botched
or altered conversion. Marked ``e2e`` so it is excluded from ``make test``
and only runs where the artifact + reference model are available.
"""

import os

import numpy as np
import pytest

pytestmark = pytest.mark.e2e

_MODEL_PATH = os.environ.get("EMBEDDING_MODEL_PATH")
_TOKENIZER_PATH = os.environ.get("EMBEDDING_TOKENIZER_PATH")
_REVISION = os.environ.get("EMBEDDING_REVISION_HASH", "")


@pytest.mark.skipif(
    not (_MODEL_PATH and _TOKENIZER_PATH and _REVISION),
    reason="mirrored artifact env vars not set",
)
def test_mirrored_artifact_matches_official_bge_m3() -> None:
    """Validate cosine similarity of ONNX-int8 vs official BGE-M3.

    Cosine similarity between our ONNX-int8 dense vectors and official
    ``BAAI/bge-m3`` dense vectors is ~0.999 on a sample.

    Why: validates conversion correctness once at adoption; a low cosine
    means the mirrored artifact drifted from the reference and must not ship.
    """
    pytest.importorskip("FlagEmbedding")
    from FlagEmbedding import (  # ty: ignore[unresolved-import]
        BGEM3FlagModel,
    )

    from qfa.adapters.embedding import build_bge_m3_embedder

    samples = (
        "The water point ran dry by midday.",
        "Le centre de sante manquait de medicaments.",
        "Vody privozili neregulyarno.",
    )
    # _MODEL_PATH and _TOKENIZER_PATH are guaranteed non-None by the skipif guard above.
    assert _MODEL_PATH is not None  # narrow type for the type checker
    assert _TOKENIZER_PATH is not None
    ours = build_bge_m3_embedder(
        model_path=_MODEL_PATH,
        tokenizer_path=_TOKENIZER_PATH,
        revision_hash=_REVISION,
    ).embed(samples)

    reference_model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
    reference = reference_model.encode(list(samples))["dense_vecs"]

    for ours_vec, ref_vec in zip(ours, reference, strict=True):
        a = np.asarray(ours_vec)
        b = np.asarray(ref_vec)
        cosine = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        assert cosine > 0.99, f"cosine {cosine:.4f} below 0.99 — bad conversion"


@pytest.mark.skipif(
    not (_MODEL_PATH and _TOKENIZER_PATH and _REVISION),
    reason="embedding artifact env vars not set",
)
def test_real_artifact_embeds_ragged_multilingual_batch() -> None:
    """The adapter runs on the real artifact and honours the dense contract.

    Why: the mirrored build emits an already-pooled ``dense_vecs`` (batch,
    1024) output and ships a tokenizer with padding/truncation disabled.
    This exercises the full ``build_bge_m3_embedder`` path on a ragged,
    multilingual batch and pins the runtime contract clustering depends on —
    one L2-normalised 1024-d float vector per input, in order, with a
    cross-lingual paraphrase landing nearer than unrelated text. Unlike the
    cosine-vs-official check it needs no torch reference model, only the
    artifact, so it catches padding and output-shape regressions cheaply
    (both of which previously made ``embed`` raise on any real batch).
    """
    assert _MODEL_PATH is not None  # narrowed by the skipif guard
    assert _TOKENIZER_PATH is not None
    from qfa.adapters.embedding import build_bge_m3_embedder

    embedder = build_bge_m3_embedder(
        model_path=_MODEL_PATH,
        tokenizer_path=_TOKENIZER_PATH,
        revision_hash=_REVISION,
    )

    # Differing token lengths exercise padding; the EN/FR pair are paraphrases.
    english = "The water point ran dry by midday."
    french = "Le point d'eau s'est asseche a midi."
    unrelated = "The football match last weekend was thrilling."
    vectors = embedder.embed((english, french, unrelated))

    # Contract: one 1024-d float vector per input, preserving input order.
    assert len(vectors) == 3
    assert all(len(v) == 1024 for v in vectors)
    assert all(isinstance(x, float) for row in vectors for x in row)

    arrays = [np.asarray(v, dtype=np.float64) for v in vectors]
    # Each vector is L2-normalised (unit length).
    for vec in arrays:
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-3

    # Cross-lingual sanity: paraphrases are nearer than unrelated text.
    # Cosine == dot product for unit vectors.
    cos_paraphrase = float(arrays[0] @ arrays[1])
    cos_unrelated = float(arrays[0] @ arrays[2])
    assert cos_paraphrase > cos_unrelated
