"""Self-hosted ONNX embedding adapter (multilingual, dense-only).

Runs a multilingual sentence-embedding model via ``onnxruntime``,
in-process, loaded once. Behind :class:`~qfa.domain.ports.EmbeddingPort`.

Two model *families* are supported; they differ only in how the ONNX
graph's output is turned into one dense vector per text:

* ``bge-m3`` — the shipped BGE-M3 build emits an already-pooled
  ``dense_vecs`` head (shape ``(batch, dim)``); the adapter takes it as-is
  (``pooling="pre_pooled"``).
* ``e5`` — multilingual-E5 ONNX exports emit token-level
  ``last_hidden_state`` (shape ``(batch, seq, hidden)``); the adapter
  **mean-pools** it over the attention mask (``pooling="mean"``) and
  prepends the ``"query: "`` prefix every E5 input requires.

The *dimension* and *token cap* are per-artifact, not per-family, so they
are separate knobs (``dense_dim`` / the builder's ``max_tokens``): both
e5-base (768-d) and e5-small (384-d) use ``model_kind="e5"``.

Security posture (asserted at construction, per the design spec):

* ``trust_remote_code=False`` — a standard-op ONNX graph cannot execute
  arbitrary code or perform I/O, unlike a pickle ``.bin`` checkpoint.
* **No** custom-operator libraries registered — custom ops can run native
  code.
* Model pinned by a **revision hash** and loaded from a **local mirrored
  artifact path** — never fetched from HuggingFace at runtime in prod.

The residual attack surface is onnxruntime parser CVEs (keep patched)
and conversion correctness (the one-time cosine~0.999 validation against
the official reference, see the e2e-marked test).

Batching & concurrency: records are embedded in sequential batches of
``batch_size`` (default 100) — one ``session.run()`` per batch — so a large
corpus never materialises one giant padded-token tensor or activation map (the
dominant memory cost, since padding is to the longest row *in the batch*).
Within a batch, ``intra_op_num_threads`` saturates cores; there is no
thread/process pool across batches.
"""

import logging
from typing import Any

import numpy as np

from qfa.domain.ports import EmbeddingPort
from qfa.settings import DEFAULT_EMBEDDING_BATCH_SIZE

logger = logging.getLogger(__name__)

# Valid output-pooling strategies, see the module docstring.
_PRE_POOLED = "pre_pooled"
_MEAN = "mean"
_POOLINGS = (_PRE_POOLED, _MEAN)

# Natural context windows per family. BGE-M3 accepts up to 8192 tokens; the
# E5 family inherits its XLM-R/MiniLM backbone's 512 positional limit. Longer
# inputs are truncated. Feedback records are short, so the cap almost never
# bites — it is a guardrail against a pathological outlier blowing up the run
# (and, since padding is per-batch, blowing up its whole batch's tensor).
_BGE_M3_MAX_TOKENS = 8192
_E5_MAX_TOKENS = 512

# model_kind -> (pooling, query_prefix, default_max_tokens). The family fixes
# the output handling; dimension and an explicit token cap are passed in.
_FAMILY: dict[str, tuple[str, str, int]] = {
    "bge-m3": (_PRE_POOLED, "", _BGE_M3_MAX_TOKENS),
    "e5": (_MEAN, "query: ", _E5_MAX_TOKENS),
}


def _mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Masked mean of token vectors over the sequence axis.

    ``last_hidden`` is ``(batch, seq, hidden)`` and ``attention_mask`` is
    ``(batch, seq)``; returns ``(batch, hidden)``. Pad positions (mask 0) are
    excluded from both the sum and the divisor, so padding to the batch's
    longest row does not perturb the result. The divisor is floored at a tiny
    epsilon so an all-pad row (no real tokens) cannot divide by zero.
    """
    mask = attention_mask[:, :, None].astype(np.float32)  # (batch, seq, 1)
    summed = (last_hidden * mask).sum(axis=1)  # (batch, hidden)
    counts = np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)  # (batch, 1)
    return summed / counts


class OnnxEmbedder(EmbeddingPort):
    """Self-hosted ONNX dense-only embedder (explicitly inherits the port)."""

    def __init__(
        self,
        *,
        model_path: str,
        revision_hash: str,
        session: Any,
        tokenizer: Any,
        pooling: str = _PRE_POOLED,
        query_prefix: str = "",
        dense_dim: int = 1024,
        trust_remote_code: bool = False,
        custom_op_libraries: tuple[str, ...] = (),
        intra_op_num_threads: int | None = None,
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
        max_tokens: int = _BGE_M3_MAX_TOKENS,
    ) -> None:
        """Construct the embedder and assert the required security flags.

        Parameters
        ----------
        model_path : str
            Filesystem path to the mirrored ONNX artifact (never a HF URL
            in production).
        revision_hash : str
            Pinned revision/content hash of the artifact. Must be non-empty.
        session : Any
            A pre-built ``onnxruntime.InferenceSession`` (or a test fake
            exposing ``run``). Injected so unit tests need no model file.
        tokenizer : Any
            A callable tokenizer returning ``{"input_ids", "attention_mask"}``
            arrays. Injected for the same reason.
        pooling : str
            ``"pre_pooled"`` (take ``outputs[0]`` as the dense vector, BGE-M3)
            or ``"mean"`` (mean-pool token-level ``last_hidden_state`` over the
            attention mask, E5). Any other value raises.
        query_prefix : str
            String prepended to every text before tokenizing (``"query: "``
            for E5; empty for BGE-M3).
        dense_dim : int
            Expected output dimensionality; each batch is validated against it
            so a wrong artifact/config fails loud.
        trust_remote_code : bool
            MUST be ``False``. Any other value raises.
        custom_op_libraries : tuple[str, ...]
            MUST be empty. Any registered library raises.
        intra_op_num_threads : int | None
            onnxruntime thread count; ``None`` leaves the core-count default.
        batch_size : int
            Number of records encoded per ``session.run`` call. The corpus is
            embedded in sequential batches of this size to bound peak memory on
            large inputs. Must be ``>= 1``.
        max_tokens : int
            The tokenizer's truncation cap. Used only to detect and warn about
            silently truncated inputs (a row whose attention-mask sum reaches
            this cap was almost certainly cut off). Must match the
            ``enable_truncation`` length the builder configured so the two stay
            in lock-step.

        Raises
        ------
        ValueError
            If a security flag is violated, ``pooling`` is unknown,
            ``revision_hash`` is empty, or ``batch_size`` is less than 1.
        """
        if trust_remote_code:
            raise ValueError("trust_remote_code must be False for OnnxEmbedder")
        if custom_op_libraries:
            raise ValueError(
                "no custom-operator libraries may be registered: "
                f"{custom_op_libraries!r}"
            )
        if pooling not in _POOLINGS:
            raise ValueError(f"pooling must be one of {_POOLINGS}, got {pooling!r}")
        if not revision_hash:
            raise ValueError("revision_hash must be a non-empty pinned hash")
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        self._model_path = model_path
        self._revision_hash = revision_hash
        self._session = session
        self._tokenizer = tokenizer
        self._pooling = pooling
        self._query_prefix = query_prefix
        self._dense_dim = dense_dim
        self._intra_op_num_threads = intra_op_num_threads
        self._batch_size = batch_size
        self._max_tokens = max_tokens
        logger.info(
            "OnnxEmbedder ready: path=%s revision=%s pooling=%s dim=%d"
            " threads=%s batch_size=%s",
            model_path,
            revision_hash,
            pooling,
            dense_dim,
            intra_op_num_threads,
            batch_size,
        )

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one dense ``dense_dim``-d vector per input text, in input order.

        Encodes the input in sequential batches of ``batch_size`` (one
        ``session.run`` per batch) and concatenates the results, so a large
        corpus never holds one giant padded-token tensor or activation map in
        memory at once. Each text is prefixed with ``query_prefix`` (empty for
        BGE-M3) before tokenizing, and the model output is reduced to one
        vector per row according to ``pooling``. Empty input returns ``()``
        without touching the model.
        """
        if not texts:
            return ()

        vectors: list[tuple[float, ...]] = []
        total_batches = (len(texts) + self._batch_size - 1) // self._batch_size
        for batch_index, start in enumerate(range(0, len(texts), self._batch_size)):
            batch = texts[start : start + self._batch_size]
            logger.debug(
                "embedding batch %d/%d (%d record(s))",
                batch_index + 1,
                total_batches,
                len(batch),
            )
            vectors.extend(self._embed_batch(batch))
        return tuple(vectors)

    def _embed_batch(self, batch: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Embed one ``<= batch_size`` slice in a single ``session.run`` call.

        Prepends ``query_prefix`` to each text, runs the model, reduces the
        output to one vector per row (``pre_pooled`` takes ``outputs[0]``
        as-is; ``mean`` mean-pools the token-level ``last_hidden_state`` over
        the attention mask), then L2-normalises each row. Padding is to the
        longest row *in this batch*, which is why batching bounds memory rather
        than just call count.
        """
        prepared = (
            [self._query_prefix + text for text in batch]
            if self._query_prefix
            else list(batch)
        )
        encoded = self._tokenizer(prepared)
        input_ids = np.asarray(encoded["input_ids"])
        attention_mask = np.asarray(encoded["attention_mask"])

        # Surface otherwise-silent truncation: the tokenizer caps inputs at
        # self._max_tokens, so a row whose real-token count (its attention-mask
        # sum) reaches the cap was almost certainly truncated and lost trailing
        # content. Log only the count and the limit — never the text — per the
        # content-free logging rule in docs/operations/observability.md.
        truncated = int(
            np.count_nonzero(attention_mask.sum(axis=1) >= self._max_tokens)
        )
        if truncated:
            logger.warning(
                "%d record(s) hit the %d-token limit and were truncated before "
                "embedding",
                truncated,
                self._max_tokens,
            )

        outputs = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        raw = np.asarray(outputs[0], dtype=np.float32)
        # BGE-M3's first output is already a pooled (batch, dim) dense vector;
        # E5's is token-level (batch, seq, hidden) and must be mean-pooled.
        dense = raw if self._pooling == _PRE_POOLED else _mean_pool(raw, attention_mask)

        # L2 normalise each row (idempotent when the export already normalised);
        # cosine similarity over unit vectors is what clustering consumes.
        norms = np.linalg.norm(dense, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-12, a_max=None)
        dense = dense / norms

        if dense.shape[1] != self._dense_dim:
            raise ValueError(
                f"expected {self._dense_dim}-d dense vectors, got {dense.shape[1]}"
            )
        return tuple(tuple(float(x) for x in row) for row in dense)


def build_onnx_embedder(
    *,
    model_kind: str,
    model_path: str,
    tokenizer_path: str,
    revision_hash: str,
    dense_dim: int,
    max_tokens: int | None = None,
    intra_op_num_threads: int | None = None,
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
) -> OnnxEmbedder:
    """Build an :class:`OnnxEmbedder` for a model family from a local artifact.

    Resolves ``model_kind`` to its pooling strategy, query prefix, and natural
    token cap, loads the ONNX session with the standard CPU provider and the
    configured thread count, and loads the tokenizer from the mirrored files.
    Imports of ``onnxruntime``/``tokenizers`` are local to this function so
    unit tests (which inject fakes) never trigger them.

    Parameters
    ----------
    model_kind : str
        ``"bge-m3"`` or ``"e5"`` — selects pooling + query prefix + the default
        token cap (see :data:`_FAMILY`).
    model_path : str
        Path to the mirrored ONNX graph.
    tokenizer_path : str
        Path to the mirrored tokenizer file.
    revision_hash : str
        Pinned artifact hash (passed through to the constructor's check).
    dense_dim : int
        Expected output dimensionality, validated per batch.
    max_tokens : int | None
        Tokenizer truncation cap; ``None`` uses the family's natural context
        (8192 for ``bge-m3``, 512 for ``e5``).
    intra_op_num_threads : int | None
        onnxruntime intra-op thread count; ``None`` keeps the core-count
        default.
    batch_size : int
        Records encoded per ``session.run`` call (memory bound for large
        corpora); passed through to the constructor.

    Raises
    ------
    ValueError
        If ``model_kind`` is not a known family.
    """
    try:
        pooling, query_prefix, default_max_tokens = _FAMILY[model_kind]
    except KeyError:
        raise ValueError(
            f"unknown model_kind {model_kind!r}; expected one of {sorted(_FAMILY)}"
        ) from None
    effective_max_tokens = max_tokens if max_tokens is not None else default_max_tokens

    import onnxruntime as ort
    from tokenizers import Tokenizer

    sess_options = ort.SessionOptions()
    if intra_op_num_threads is not None:
        sess_options.intra_op_num_threads = intra_op_num_threads
    session = ort.InferenceSession(
        model_path,
        sess_options=sess_options,
        providers=["CPUExecutionProvider"],
    )

    hf_tokenizer = Tokenizer.from_file(tokenizer_path)

    # The mirrored ``tokenizer.json`` ships with padding and truncation
    # disabled, so ``encode_batch`` returns ragged sequences and the
    # ``np.array([...])`` below raises on any batch of differing-length
    # texts. Enable both explicitly: dynamic padding to the batch's longest
    # row (no fixed waste) and truncation at the family's context limit.
    # Pooling masks the pad positions via ``attention_mask``, so the pad token
    # id does not affect the output vectors — we still set the model's real
    # pad token when present for correctness.
    pad_id = hf_tokenizer.token_to_id("<pad>")
    if pad_id is None:
        pad_id = 0
    pad_token = hf_tokenizer.id_to_token(pad_id) or "<pad>"
    hf_tokenizer.enable_truncation(max_length=effective_max_tokens)
    hf_tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)

    def _tokenize(batch: list[str]) -> dict[str, "np.ndarray"]:
        encodings = hf_tokenizer.encode_batch(batch)
        input_ids = np.array([e.ids for e in encodings])
        attention_mask = np.array([e.attention_mask for e in encodings])
        return {"input_ids": input_ids, "attention_mask": attention_mask}

    return OnnxEmbedder(
        model_path=model_path,
        revision_hash=revision_hash,
        session=session,
        tokenizer=_tokenize,
        pooling=pooling,
        query_prefix=query_prefix,
        dense_dim=dense_dim,
        trust_remote_code=False,
        custom_op_libraries=(),
        intra_op_num_threads=intra_op_num_threads,
        batch_size=batch_size,
        max_tokens=effective_max_tokens,
    )


def build_bge_m3_embedder(
    *,
    model_path: str,
    tokenizer_path: str,
    revision_hash: str,
    intra_op_num_threads: int | None = None,
    batch_size: int = 100,
) -> OnnxEmbedder:
    """Build the BGE-M3 (1024-d, pre-pooled) embedder — a thin family wrapper.

    Kept as the named entry point for the BGE-M3 path (used by the e2e
    artifact-validation test); delegates to :func:`build_onnx_embedder` with
    ``model_kind="bge-m3"`` and the model's 1024-d / 8192-token defaults.
    """
    return build_onnx_embedder(
        model_kind="bge-m3",
        model_path=model_path,
        tokenizer_path=tokenizer_path,
        revision_hash=revision_hash,
        dense_dim=1024,
        max_tokens=None,
        intra_op_num_threads=intra_op_num_threads,
        batch_size=batch_size,
    )
