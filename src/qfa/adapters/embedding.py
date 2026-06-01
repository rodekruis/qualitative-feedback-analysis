"""Self-hosted BGE-M3 ONNX-int8 embedding adapter.

Runs BGE-M3 (multilingual, dense-1024-d only) via ``onnxruntime``,
in-process, loaded once. Behind :class:`~qfa.domain.ports.EmbeddingPort`.

Security posture (asserted at construction, per the design spec):

* ``trust_remote_code=False`` — a standard-op ONNX graph cannot execute
  arbitrary code or perform I/O, unlike a pickle ``.bin`` checkpoint.
* **No** custom-operator libraries registered — custom ops can run native
  code.
* Model pinned by a **revision hash** and loaded from a **local mirrored
  artifact path** — never fetched from HuggingFace at runtime in prod.

The residual attack surface is onnxruntime parser CVEs (keep patched)
and conversion correctness (the one-time cosine~0.999 validation against
official ``BAAI/bge-m3``, see the e2e-marked test).

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

logger = logging.getLogger(__name__)

_DENSE_DIM = 1024

# BGE-M3 accepts up to 8192 tokens; longer inputs are truncated. Feedback
# records are short, so this almost never bites — it's a guardrail against a
# pathological outlier blowing up the ONNX run.
_MAX_TOKENS = 8192


class BgeM3OnnxEmbedder(EmbeddingPort):
    """BGE-M3 ONNX-int8 dense-only embedder (explicitly inherits the port)."""

    def __init__(
        self,
        *,
        model_path: str,
        revision_hash: str,
        session: Any,
        tokenizer: Any,
        trust_remote_code: bool = False,
        custom_op_libraries: tuple[str, ...] = (),
        intra_op_num_threads: int | None = None,
        batch_size: int = 100,
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

        Raises
        ------
        ValueError
            If a security flag is violated, ``revision_hash`` is empty, or
            ``batch_size`` is less than 1.
        """
        if trust_remote_code:
            raise ValueError("trust_remote_code must be False for BgeM3OnnxEmbedder")
        if custom_op_libraries:
            raise ValueError(
                "no custom-operator libraries may be registered: "
                f"{custom_op_libraries!r}"
            )
        if not revision_hash:
            raise ValueError("revision_hash must be a non-empty pinned hash")
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        self._model_path = model_path
        self._revision_hash = revision_hash
        self._session = session
        self._tokenizer = tokenizer
        self._intra_op_num_threads = intra_op_num_threads
        self._batch_size = batch_size
        logger.info(
            "BgeM3OnnxEmbedder ready: path=%s revision=%s threads=%s batch_size=%s",
            model_path,
            revision_hash,
            intra_op_num_threads,
            batch_size,
        )

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one dense 1024-d vector per input text, in input order.

        Encodes the input in sequential batches of ``batch_size`` (one
        ``session.run`` per batch) and concatenates the results, so a large
        corpus never holds one giant padded-token tensor or activation map in
        memory at once. Within a batch the shipped BGE-M3 ONNX build emits its
        already-pooled ``dense_vecs`` head — shape ``(batch, 1024)`` — so there
        is no token-dimension pooling to do here. Empty input returns ``()``
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

        L2 normalises each row of the model's first output (``dense_vecs``).
        Padding is to the longest row *in this batch*, which is why batching
        bounds memory rather than just call count.
        """
        encoded = self._tokenizer(list(batch))
        input_ids = np.asarray(encoded["input_ids"])
        attention_mask = np.asarray(encoded["attention_mask"])

        outputs = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        # First output is dense_vecs: a pooled (batch, 1024) dense vector.
        dense = np.asarray(outputs[0], dtype=np.float32)

        # L2 normalise each row (idempotent when the export already normalised).
        norms = np.linalg.norm(dense, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-12, a_max=None)
        dense = dense / norms

        if dense.shape[1] != _DENSE_DIM:
            raise ValueError(
                f"expected {_DENSE_DIM}-d dense vectors, got {dense.shape[1]}"
            )
        return tuple(tuple(float(x) for x in row) for row in dense)


def build_bge_m3_embedder(
    *,
    model_path: str,
    tokenizer_path: str,
    revision_hash: str,
    intra_op_num_threads: int | None = None,
    batch_size: int = 100,
) -> BgeM3OnnxEmbedder:
    """Build a :class:`BgeM3OnnxEmbedder` from the mirrored local artifact.

    Loads the ONNX session with the standard CPU provider and the
    configured thread count, and loads the tokenizer from the mirrored
    files. Imports of ``onnxruntime``/``tokenizers`` are local to this
    function so unit tests (which inject fakes) never trigger them.

    Parameters
    ----------
    model_path : str
        Path to the mirrored ``model.onnx``.
    tokenizer_path : str
        Path to the mirrored tokenizer directory/file.
    revision_hash : str
        Pinned artifact hash (passed through to the constructor's check).
    intra_op_num_threads : int | None
        onnxruntime intra-op thread count; ``None`` keeps the core-count
        default.
    batch_size : int
        Records encoded per ``session.run`` call (memory bound for large
        corpora); passed through to the constructor.
    """
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
    # row (no fixed waste) and truncation at the model's context limit.
    # Mean-pooling already masks the pad positions via ``attention_mask``,
    # so the pad token id does not affect the output vectors — we still set
    # the model's real pad token when present for correctness.
    pad_id = hf_tokenizer.token_to_id("<pad>")
    if pad_id is None:
        pad_id = 0
    pad_token = hf_tokenizer.id_to_token(pad_id) or "<pad>"
    hf_tokenizer.enable_truncation(max_length=_MAX_TOKENS)
    hf_tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)

    def _tokenize(batch: list[str]) -> dict[str, "np.ndarray"]:
        encodings = hf_tokenizer.encode_batch(batch)
        input_ids = np.array([e.ids for e in encodings])
        attention_mask = np.array([e.attention_mask for e in encodings])
        return {"input_ids": input_ids, "attention_mask": attention_mask}

    return BgeM3OnnxEmbedder(
        model_path=model_path,
        revision_hash=revision_hash,
        session=session,
        tokenizer=_tokenize,
        trust_remote_code=False,
        custom_op_libraries=(),
        intra_op_num_threads=intra_op_num_threads,
        batch_size=batch_size,
    )
