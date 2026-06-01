"""Tests for the ONNX embedding adapter (BGE-M3 and E5 families).

Why: the adapter is the one new external dependency behind ``EmbeddingPort``.
These tests pin (1) the security posture asserted at construction
(trust_remote_code disabled, no custom-op libs, a pinned revision hash,
a local artifact path), (2) the dense-only, input-ordered, fixed-width
output contract, and (3) the per-family output handling — BGE-M3's
already-pooled head taken as-is vs E5's masked mean-pool of token vectors
plus its ``query: `` prefix — all without downloading a model, by injecting
a fake session and tokenizer. The real-model cosine validation lives in a
separate e2e-marked test.
"""

import math
from typing import Any

import numpy as np
import pytest

from qfa.adapters.embedding import OnnxEmbedder
from qfa.domain.ports import EmbeddingPort


class _FakeTokenizer:
    """Returns deterministic fixed-length token id arrays per text."""

    def __call__(self, texts):
        # One row per text, padded to length 4.
        ids = [[1, 2, 3, 0] for _ in texts]
        mask = [[1, 1, 1, 0] for _ in texts]
        return {"input_ids": np.array(ids), "attention_mask": np.array(mask)}


class _RecordingTokenizer:
    """Records the exact texts it was called with (to assert prefixing)."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, texts):
        self.seen = list(texts)
        ids = [[1, 2, 3, 0] for _ in texts]
        mask = [[1, 1, 1, 0] for _ in texts]
        return {"input_ids": np.array(ids), "attention_mask": np.array(mask)}


class _FakeSession:
    """Fake onnxruntime session emitting an already-pooled dense head.

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


class _FakeTokenLevelSession:
    """Fake session emitting token-level ``last_hidden_state``.

    Mirrors an E5 ONNX export: first output is ``(batch, seq, hidden)`` and
    must be mean-pooled over the attention mask. Returns a fixed tensor so the
    masked-mean arithmetic is exactly checkable.
    """

    def __init__(self, last_hidden: np.ndarray) -> None:
        self.run_calls = 0
        self._last_hidden = last_hidden

    def run(self, output_names, inputs):
        self.run_calls += 1
        return [self._last_hidden]


def _make_embedder(
    model_path: str = "/srv/models/bge-m3-onnx-int8/model.onnx",
    revision_hash: str = "sha256:deadbeef",
    session: Any = None,
    tokenizer: Any = None,
    pooling: str = "pre_pooled",
    query_prefix: str = "",
    dense_dim: int = 1024,
    trust_remote_code: bool = False,
    custom_op_libraries: tuple[str, ...] = (),
    intra_op_num_threads: int | None = 4,
    batch_size: int = 100,
) -> OnnxEmbedder:
    """Build an ``OnnxEmbedder`` with sensible defaults for unit tests."""
    return OnnxEmbedder(
        model_path=model_path,
        revision_hash=revision_hash,
        session=session if session is not None else _FakeSession(),
        tokenizer=tokenizer if tokenizer is not None else _FakeTokenizer(),
        pooling=pooling,
        query_prefix=query_prefix,
        dense_dim=dense_dim,
        trust_remote_code=trust_remote_code,
        custom_op_libraries=custom_op_libraries,
        intra_op_num_threads=intra_op_num_threads,
        batch_size=batch_size,
    )


def test_adapter_inherits_embedding_port() -> None:
    """``OnnxEmbedder`` explicitly inherits ``EmbeddingPort`` (AGENTS.md rule).

    Why: the explicit base class makes the port<->adapter link discoverable
    in IDEs; structural conformance alone is reserved for test fakes.
    """
    assert issubclass(OnnxEmbedder, EmbeddingPort)


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


def test_construction_rejects_unknown_pooling() -> None:
    """An unrecognised ``pooling`` strategy raises at construction.

    Why: pooling selects how the ONNX output becomes a vector; a typo'd or
    unsupported value must fail loud rather than silently mishandle the
    model's output shape.
    """
    with pytest.raises(ValueError, match="pooling"):
        _make_embedder(pooling="max")


def test_embed_returns_one_normalised_1024d_vector_per_text_in_order() -> None:
    """``embed`` returns one L2-normalised dense 1024-d vector per input, in order.

    Why: clustering depends on a fixed-width, order-preserving contract of
    unit vectors; the shipped BGE-M3 build's already-pooled ``dense_vecs`` is
    taken as-is (no token pooling) and L2-normalised, and BGE-M3's
    sparse/ColBERT outputs are explicitly unused.
    """
    embedder = _make_embedder()
    vectors = embedder.embed(("first feedback", "second feedback"))
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)
    assert all(isinstance(x, float) for x in vectors[0])
    # ones/sqrt(1024) is the L2-normalised unit vector -> norm 1.0.
    assert abs(math.sqrt(sum(x * x for x in vectors[0])) - 1.0) < 1e-5


def test_mean_pooling_averages_token_vectors_over_attention_mask() -> None:
    """``pooling="mean"`` masked-means token vectors, ignoring pad positions.

    Why: E5 exports emit token-level ``last_hidden_state``; the adapter must
    average over real tokens only (mask=1) and exclude padding, then
    L2-normalise. A pad token carrying a huge vector must not leak into the
    result — otherwise per-batch padding would corrupt every vector.
    """
    # batch=1, seq=3, hidden=4. Tokens 0,1 are real; token 2 is padding and
    # carries a large vector that must be excluded by the mask.
    last_hidden = np.array(
        [[[2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0], [9.0, 9.0, 9.0, 9.0]]],
        dtype=np.float32,
    )

    class _MaskTokenizer:
        def __call__(self, texts):
            return {
                "input_ids": np.array([[10, 11, 0]]),
                "attention_mask": np.array([[1, 1, 0]]),
            }

    embedder = _make_embedder(
        session=_FakeTokenLevelSession(last_hidden),
        tokenizer=_MaskTokenizer(),
        pooling="mean",
        dense_dim=4,
    )
    (vector,) = embedder.embed(("one record",))
    # masked mean of the two real tokens = [1, 1, 0, 0]; L2-normalised.
    expected = [1 / math.sqrt(2), 1 / math.sqrt(2), 0.0, 0.0]
    assert all(abs(got - exp) < 1e-6 for got, exp in zip(vector, expected, strict=True))


def test_query_prefix_is_prepended_before_tokenizing() -> None:
    """``query_prefix`` is prepended to every text before it reaches the tokenizer.

    Why: E5 models are trained with a ``query: ``/``passage: `` prefix and
    degrade without it; for symmetric clustering every record gets the same
    ``query: `` prefix. The prefix must be applied at the tokenizer boundary,
    not left to the caller.
    """
    recorder = _RecordingTokenizer()
    embedder = _make_embedder(tokenizer=recorder, query_prefix="query: ")
    embedder.embed(("water point dry", "clinic out of stock"))
    assert recorder.seen == ["query: water point dry", "query: clinic out of stock"]


def test_no_prefix_passes_text_through_unchanged() -> None:
    """An empty ``query_prefix`` (BGE-M3) leaves the text untouched.

    Why: BGE-M3 takes no prefix; prepending one would shift every vector and
    silently degrade clustering. The default must be a true pass-through.
    """
    recorder = _RecordingTokenizer()
    embedder = _make_embedder(tokenizer=recorder, query_prefix="")
    embedder.embed(("verbatim text",))
    assert recorder.seen == ["verbatim text"]


def test_embed_rejects_dense_vectors_of_the_wrong_width() -> None:
    """A model output whose width != ``dense_dim`` raises rather than passing through.

    Why: ``dense_dim`` is the per-artifact contract; a mismatched artifact or
    misconfigured dimension must fail loud, not feed wrong-width vectors into
    clustering. Here the fake emits 1024-d but the embedder expects 768.
    """
    embedder = _make_embedder(dense_dim=768)  # fake session emits 1024-d
    with pytest.raises(ValueError, match="768-d"):
        embedder.embed(("mismatch",))


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


def test_build_onnx_embedder_rejects_unknown_model_kind() -> None:
    """``build_onnx_embedder`` rejects an unknown family before loading anything.

    Why: ``model_kind`` selects pooling + prefix; an unknown value must fail
    fast with a clear message, not after paying to load an ONNX session — the
    family lookup happens before any artifact IO.
    """
    from qfa.adapters.embedding import build_onnx_embedder

    with pytest.raises(ValueError, match="unknown model_kind"):
        build_onnx_embedder(
            model_kind="bogus",
            model_path="/nonexistent.onnx",
            tokenizer_path="/nonexistent.json",
            revision_hash="sha256:abc",
            dense_dim=768,
        )
