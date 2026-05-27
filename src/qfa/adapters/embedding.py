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

Concurrency: a single batched ``session.run()`` already saturates cores
via ``intra_op_num_threads``; there is no thread/process pool.
"""

import logging
from typing import Any

import numpy as np

from qfa.domain.ports import EmbeddingPort

logger = logging.getLogger(__name__)

_DENSE_DIM = 1024


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

        Raises
        ------
        ValueError
            If a security flag is violated or ``revision_hash`` is empty.
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

        self._model_path = model_path
        self._revision_hash = revision_hash
        self._session = session
        self._tokenizer = tokenizer
        self._intra_op_num_threads = intra_op_num_threads
        logger.info(
            "BgeM3OnnxEmbedder ready: path=%s revision=%s threads=%s",
            model_path,
            revision_hash,
            intra_op_num_threads,
        )

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one dense 1024-d vector per input text, in input order.

        Encodes the whole batch in a single ``session.run`` call. Uses
        attention-mask mean pooling over the token dimension, then L2
        normalises each vector (the standard BGE-M3 dense retrieval recipe).
        Empty input returns ``()`` without touching the model.
        """
        if not texts:
            return ()

        encoded = self._tokenizer(list(texts))
        input_ids = np.asarray(encoded["input_ids"])
        attention_mask = np.asarray(encoded["attention_mask"])

        outputs = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        token_embeddings = np.asarray(outputs[0], dtype=np.float32)

        # Mean-pool over tokens using the attention mask.
        mask = attention_mask[:, :, None].astype(np.float32)
        summed = (token_embeddings * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), a_min=1.0, a_max=None)
        pooled = summed / counts

        # L2 normalise.
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-12, a_max=None)
        dense = pooled / norms

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
    )
