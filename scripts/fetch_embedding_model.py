r"""Download a self-hosted ONNX embedding model for local dev.

Helper script (run manually, not in CI). Hierarchical analysis
(``mode=hierarchical`` on ``POST /v1/analyze``, and the in-process calls
the ``notebooks/analyze_corpus.ipynb`` notebook makes) needs a local ONNX
embedding artifact wired up via env vars. Without them the orchestrator
carries ``_embedder is None`` and the hierarchical path raises ``no
embedder configured``.

This script fetches the two files the adapter actually loads
(:func:`qfa.adapters.embedding.build_onnx_embedder` opens the ONNX graph +
the fast-tokenizer JSON; the rest of the HF repo is unused at runtime),
drops them in a gitignored default location under the repo, and prints the
``.env`` lines to paste — including ``EMBEDDING_MODEL_KIND`` and
``EMBEDDING_DENSE_DIM`` so the adapter handles the model's output correctly.

Supported models (``--model``):

* ``e5-base`` (default) — intfloat/multilingual-e5-base, 768-d, mean-pooled.
  The default model: smaller and faster than BGE-M3 for a modest
  cross-lingual quality trade.
* ``e5-small`` — intfloat/multilingual-e5-small, 384-d, mean-pooled. Smallest
  and fastest; the largest quality trade.
* ``bge-m3`` — BAAI BGE-M3, 1024-d, pre-pooled dense head. The strongest
  cross-lingual model; select it when quality matters more than latency.

**Dev-only — not the production path.** Per ADR-014 production never
fetches from HuggingFace at runtime: the artifact is mirrored to an
internal store and the path/hash are injected as config. This script is
the developer-machine convenience that produces an equivalent local
artifact, pinned to the same commit so its content matches
``EMBEDDING_REVISION_HASH``.

Rate-limit safety: HuggingFace 429s easily under concurrent fetches, so
downloads are serialized — ``max_workers=1`` (one file at a time) and
``HF_HUB_ENABLE_HF_TRANSFER=0`` (no parallel-chunk acceleration of a single
file). We only need two files, so the serial path costs nothing.

Run::

    uv run python scripts/fetch_embedding_model.py                 # e5-base
    uv run python scripts/fetch_embedding_model.py --model bge-m3

Then copy the printed ``EMBEDDING_*`` lines into your ``.env`` (or export
them) and restart any running server / Jupyter kernel.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("fetch_embedding_model")

# The script lives at ``scripts/`` so the repo root is one level up.
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ModelSpec:
    """A downloadable embedding artifact and the config it implies.

    ``onnx_relpath``/``tokenizer_relpath`` are paths *within* the HF repo
    (BGE-M3 ships the graph at the repo root; the E5 repos nest it under
    ``onnx/``). ``model_kind``/``dense_dim``/``max_tokens`` are the
    ``EMBEDDING_*`` values that make the adapter handle this model correctly:
    the family selects pooling + query prefix, the dimension is validated per
    batch, and ``max_tokens`` (``None`` = the family's natural context) caps
    truncation.
    """

    key: str
    repo_id: str
    revision: str
    dest_dirname: str
    onnx_relpath: str
    tokenizer_relpath: str
    model_kind: str
    dense_dim: int
    max_tokens: int | None


# Revisions are pinned to a concrete commit SHA (HF ``main`` is a moving ref)
# so a download is reproducible and its content matches the printed
# EMBEDDING_REVISION_HASH. To bump one: pick a newer commit, re-validate, and
# update here (keep entries in sync with the Dockerfile's ARGs).
MODELS: dict[str, ModelSpec] = {
    "e5-base": ModelSpec(
        key="e5-base",
        # Official repo ships its own ONNX exports, including an int8 build —
        # better provenance than a third-party conversion, matching canonical
        # weights for the cosine-validation story.
        repo_id="intfloat/multilingual-e5-base",
        revision="d128750597153bb5987e10b1c3493a34e5a4502a",
        dest_dirname="multilingual-e5-base",
        onnx_relpath="onnx/model_qint8_avx512_vnni.onnx",
        tokenizer_relpath="tokenizer.json",
        model_kind="e5",
        dense_dim=768,
        max_tokens=512,
    ),
    "e5-small": ModelSpec(
        key="e5-small",
        repo_id="intfloat/multilingual-e5-small",
        revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        dest_dirname="multilingual-e5-small",
        onnx_relpath="onnx/model_qint8_avx512_vnni.onnx",
        tokenizer_relpath="tokenizer.json",
        model_kind="e5",
        dense_dim=384,
        max_tokens=512,
    ),
    "bge-m3": ModelSpec(
        key="bge-m3",
        # Community pre-built ONNX-int8 export of ``BAAI/bge-m3`` (see ADR-014).
        repo_id="gpahal/bge-m3-onnx-int8",
        revision="2b34e84df040034d4b9eabb62383a87c18955822",
        dest_dirname="bge-m3-onnx-int8",
        # NOTE: optimum names the quantized graph ``<name>_quantized.onnx``,
        # *not* ``model.onnx`` — a relpath mismatch here downloads nothing.
        onnx_relpath="model_quantized.onnx",
        tokenizer_relpath="tokenizer.json",
        model_kind="bge-m3",
        dense_dim=1024,
        max_tokens=None,
    ),
}

DEFAULT_MODEL = "e5-base"


def fetch(
    *,
    repo_id: str,
    revision: str,
    onnx_relpath: str,
    tokenizer_relpath: str,
    dest: Path,
) -> tuple[Path, Path, str]:
    """Download the ONNX graph + tokenizer into ``dest``, serially.

    Resolves ``revision`` to a concrete commit SHA (so a branch name like
    ``main`` becomes a real content pin) and downloads only the two needed
    files with ``max_workers=1`` to stay under HuggingFace rate limits.

    Parameters
    ----------
    repo_id : str
        HuggingFace repo, e.g. ``intfloat/multilingual-e5-base``.
    revision : str
        Commit SHA, tag, or branch. Resolved to a SHA for the returned hash.
    onnx_relpath, tokenizer_relpath : str
        Paths to the two files *within* the repo (the E5 repos nest the graph
        under ``onnx/``; BGE-M3 ships it at the root).
    dest : Path
        Target directory; created if absent. Repo structure is preserved
        beneath it, so a nested ``onnx_relpath`` lands at ``dest/onnx/...``.

    Returns
    -------
    tuple[Path, Path, str]
        ``(model_path, tokenizer_path, resolved_sha)``.
    """
    # Force the classic single-stream download path so max_workers=1 actually
    # serializes the transfer and we stay under HuggingFace rate limits. Two
    # *independent* accelerators do parallel/chunked fetches and must both be
    # disabled (each is read at import time, so set them before the import):
    #   * Xet (hf_xet) — content-addressed chunk protocol, the default backend
    #     for many repos now. Fires many parallel range requests *per file*;
    #     the usual cause of 429s / stalls on a large artifact like this one.
    #   * hf_transfer — the older Rust parallel-chunk accelerator for one file.
    # Disabling hf_transfer alone leaves Xet's in-file parallelism untouched,
    # which max_workers (a file-level pool) cannot rein in.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

    from huggingface_hub import HfApi, snapshot_download

    resolved_sha = HfApi().repo_info(repo_id, revision=revision).sha
    logger.info("Resolved %s@%s -> %s", repo_id, revision, resolved_sha)

    dest.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s + %s (serial)", onnx_relpath, tokenizer_relpath)
    snapshot_download(
        repo_id=repo_id,
        revision=resolved_sha,
        allow_patterns=[onnx_relpath, tokenizer_relpath],
        local_dir=str(dest),
        max_workers=1,  # serialize file downloads — avoid HF 429 rate limits
    )

    model_path = dest / onnx_relpath
    tokenizer_path = dest / tokenizer_relpath
    missing = [
        rel
        for rel, p in ((onnx_relpath, model_path), (tokenizer_relpath, tokenizer_path))
        if not p.is_file()
    ]
    if missing:
        raise RuntimeError(
            f"download finished but expected files are missing: {missing} "
            f"(in {dest}) — check the repo still ships them under those names"
        )
    return model_path, tokenizer_path, resolved_sha


def main() -> int:
    """Parse args, download the chosen model, and print the ``.env`` lines."""
    parser = argparse.ArgumentParser(
        description="Download a dev ONNX embedding model from HuggingFace.",
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODELS),
        default=DEFAULT_MODEL,
        help=f"Which model family/size to fetch (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Target directory (default: .models/<model> under the repo).",
    )
    parser.add_argument(
        "--repo-id",
        default=None,
        help="Override the HuggingFace repo id (default: the model's pinned repo).",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Commit SHA, tag, or branch to pin (default: the model's vetted commit).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    spec = MODELS[args.model]
    dest = (args.dest or (REPO_ROOT / ".models" / spec.dest_dirname)).resolve()
    model_path, tokenizer_path, sha = fetch(
        repo_id=args.repo_id or spec.repo_id,
        revision=args.revision or spec.revision,
        onnx_relpath=spec.onnx_relpath,
        tokenizer_relpath=spec.tokenizer_relpath,
        dest=dest,
    )

    print()
    print("Done. Add these to your .env (or export them), then restart the")
    print("server / Jupyter kernel:")
    print()
    print(f"EMBEDDING_MODEL_PATH={model_path}")
    print(f"EMBEDDING_TOKENIZER_PATH={tokenizer_path}")
    print(f"EMBEDDING_REVISION_HASH={sha}")
    print(f"EMBEDDING_MODEL_KIND={spec.model_kind}")
    print(f"EMBEDDING_DENSE_DIM={spec.dense_dim}")
    if spec.max_tokens is not None:
        print(f"EMBEDDING_MAX_TOKENS={spec.max_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
