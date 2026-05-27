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
