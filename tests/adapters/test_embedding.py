"""Tests for the BGE-M3 ONNX embedding adapter.

Why: the adapter is the one new external dependency behind ``EmbeddingPort``.
These tests pin (1) the security posture asserted at construction
(trust_remote_code disabled, no custom-op libs, a pinned revision hash,
a local artifact path), and (2) the dense-only, input-ordered, fixed-width
output contract — all without downloading a model, by injecting a fake
session and tokenizer. The real-model cosine validation lives in a separate
e2e-marked test.
"""

from typing import Any

import numpy as np
import pytest

from qfa.adapters.embedding import BgeM3OnnxEmbedder
from qfa.domain.ports import EmbeddingPort


class _FakeTokenizer:
    """Returns deterministic fixed-length token id arrays per text."""

    def __call__(self, texts):
        # One row per text, padded to length 4.
        ids = [[1, 2, 3, 0] for _ in texts]
        mask = [[1, 1, 1, 0] for _ in texts]
        return {"input_ids": np.array(ids), "attention_mask": np.array(mask)}


class _FakeSession:
    """Fake onnxruntime session emitting the shipped model's first output.

    Mirrors the BGE-M3 ONNX build, whose first output ``dense_vecs`` is the
    already-pooled (CLS-pooled internally) dense vector — shape
    ``(batch, 1024)``, no token dimension.
    """

    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, output_names, inputs):
        self.run_calls += 1
        batch = inputs["input_ids"].shape[0]
        return [np.ones((batch, 1024), dtype=np.float32)]


def _make_embedder(
    model_path: str = "/srv/models/bge-m3-onnx-int8/model.onnx",
    revision_hash: str = "sha256:deadbeef",
    session: Any = None,
    tokenizer: Any = None,
    trust_remote_code: bool = False,
    custom_op_libraries: tuple[str, ...] = (),
    intra_op_num_threads: int | None = 4,
    batch_size: int = 100,
) -> BgeM3OnnxEmbedder:
    """Build a ``BgeM3OnnxEmbedder`` with sensible defaults for unit tests."""
    return BgeM3OnnxEmbedder(
        model_path=model_path,
        revision_hash=revision_hash,
        session=session if session is not None else _FakeSession(),
        tokenizer=tokenizer if tokenizer is not None else _FakeTokenizer(),
        trust_remote_code=trust_remote_code,
        custom_op_libraries=custom_op_libraries,
        intra_op_num_threads=intra_op_num_threads,
        batch_size=batch_size,
    )


def test_adapter_inherits_embedding_port() -> None:
    """``BgeM3OnnxEmbedder`` explicitly inherits ``EmbeddingPort`` (AGENTS.md rule).

    Why: the explicit base class makes the port<->adapter link discoverable
    in IDEs; structural conformance alone is reserved for test fakes.
    """
    assert issubclass(BgeM3OnnxEmbedder, EmbeddingPort)


def test_construction_rejects_trust_remote_code_true() -> None:
    """Constructing with ``trust_remote_code=True`` raises.

    Why: a standard-op ONNX graph cannot execute arbitrary code; enabling
    remote code re-opens that attack surface and is forbidden by the spec.
    """
    with pytest.raises(ValueError, match="trust_remote_code"):
        _make_embedder(trust_remote_code=True)


def test_construction_rejects_custom_op_libraries() -> None:
    """Registering any custom-operator library raises at construction.

    Why: custom ops can run native code; the spec forbids them so the
    residual surface is only parser CVEs (handled by patching).
    """
    with pytest.raises(ValueError, match="custom"):
        _make_embedder(custom_op_libraries=("libevil.so",))


def test_construction_requires_pinned_revision_hash() -> None:
    """A missing/empty revision hash raises.

    Why: the artifact must be pinned by hash and mirrored, never fetched
    floating from HuggingFace at runtime.
    """
    with pytest.raises(ValueError, match="revision_hash"):
        _make_embedder(revision_hash="")


def test_embed_returns_one_normalised_1024d_vector_per_text_in_order() -> None:
    """``embed`` returns one L2-normalised dense 1024-d vector per input, in order.

    Why: clustering depends on a fixed-width, order-preserving contract of
    unit vectors; the shipped build's already-pooled ``dense_vecs`` is taken
    as-is (no token pooling) and L2-normalised, and BGE-M3's sparse/ColBERT
    outputs are explicitly unused.
    """
    import math

    embedder = _make_embedder()
    vectors = embedder.embed(("first feedback", "second feedback"))
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)
    assert all(isinstance(x, float) for x in vectors[0])
    # ones/sqrt(1024) is the L2-normalised unit vector -> norm 1.0.
    assert abs(math.sqrt(sum(x * x for x in vectors[0])) - 1.0) < 1e-5


def test_embed_uses_one_session_run_when_input_fits_one_batch() -> None:
    """An input no larger than ``batch_size`` is encoded in one ``session.run``.

    Why: batching only splits inputs that exceed ``batch_size``; a small batch
    must still be a single onnxruntime call (no per-record overhead, cores
    saturated via intra_op_num_threads rather than a thread/process pool).
    """
    session = _FakeSession()
    embedder = _make_embedder(session=session, batch_size=100)
    embedder.embed(("a", "b", "c"))
    assert session.run_calls == 1


def test_embed_batches_large_input_into_sequential_session_runs() -> None:
    """Inputs beyond ``batch_size`` are encoded in sequential ``session.run`` calls.

    Why: embedding a whole large corpus in one run materialises one giant
    padded-token tensor and activation map — the dominant memory cost. Batching
    caps peak memory; the one-vector-per-text, input-order contract must survive
    the split, and the call count must be ``ceil(n / batch_size)``.
    """
    session = _FakeSession()
    embedder = _make_embedder(session=session, batch_size=2)
    vectors = embedder.embed(("a", "b", "c", "d", "e"))
    assert session.run_calls == 3  # ceil(5 / 2)
    assert len(vectors) == 5
    assert all(len(v) == 1024 for v in vectors)


def test_construction_rejects_non_positive_batch_size() -> None:
    """A ``batch_size`` below 1 raises at construction.

    Why: a zero/negative batch would embed no records per run (an infinite or
    empty loop); the contract requires at least one record per ``session.run``.
    """
    with pytest.raises(ValueError, match="batch_size"):
        _make_embedder(batch_size=0)


def test_embed_empty_input_returns_empty_without_calling_session() -> None:
    """Embedding an empty tuple returns ``()`` and never touches the model.

    Why: an empty batch is a valid degenerate case; it must not crash or
    invoke onnxruntime.
    """
    session = _FakeSession()
    embedder = _make_embedder(session=session)
    assert embedder.embed(()) == ()
    assert session.run_calls == 0
