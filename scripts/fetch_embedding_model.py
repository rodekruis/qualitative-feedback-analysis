r"""Download the self-hosted BGE-M3 ONNX-int8 embedding model for local dev.

Helper script (run manually, not in CI). Hierarchical analysis
(``mode=hierarchical`` on ``POST /v1/analyze``, and the in-process calls
the ``notebooks/analyze_corpus.ipynb`` notebook makes) needs a local
BGE-M3 ONNX export wired up via three env vars — ``EMBEDDING_MODEL_PATH``,
``EMBEDDING_TOKENIZER_PATH``, ``EMBEDDING_REVISION_HASH``. Without them the
orchestrator carries ``_embedder is None`` and the hierarchical path raises
``no embedder configured``.

This script fetches the two files the adapter actually loads
(:func:`qfa.adapters.embedding.build_bge_m3_embedder` opens the quantized
ONNX graph + the fast-tokenizer JSON; the rest of the HF repo is unused at
runtime), drops them in a gitignored default location under the repo, and
prints the three ``.env`` lines to paste.

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

    uv run python scripts/fetch_embedding_model.py

Then copy the printed ``EMBEDDING_*`` lines into your ``.env`` (or export
them) and restart any running server / Jupyter kernel.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("fetch_embedding_model")

# The script lives at ``scripts/`` so the repo root is one level up.
REPO_ROOT = Path(__file__).resolve().parents[1]

# Community pre-built ONNX-int8 export of ``BAAI/bge-m3`` (see ADR-014). We
# pin a specific commit so the download is reproducible and its content
# matches the EMBEDDING_REVISION_HASH printed below; bump deliberately.
DEFAULT_REPO_ID = "gpahal/bge-m3-onnx-int8"
DEFAULT_REVISION = "2b34e84df040034d4b9eabb62383a87c18955822"

# The two files the runtime adapter loads. NOTE: the ONNX graph in this repo
# is ``model_quantized.onnx`` (optimum's ``<name>_quantized.onnx`` naming),
# *not* ``model.onnx`` — an allow-pattern mismatch here downloads nothing.
ONNX_FILENAME = "model_quantized.onnx"
TOKENIZER_FILENAME = "tokenizer.json"

# Gitignored default location, kept under the repo so the paths are easy to
# find and reason about. Mirrors the ``.models/bge-m3-onnx-int8`` default
# documented in ``.env.example``.
DEFAULT_DEST = REPO_ROOT / ".models" / "bge-m3-onnx-int8"


def fetch(*, repo_id: str, revision: str, dest: Path) -> tuple[Path, Path, str]:
    """Download the ONNX graph + tokenizer into ``dest``, serially.

    Resolves ``revision`` to a concrete commit SHA (so a branch name like
    ``main`` becomes a real content pin) and downloads only the two needed
    files with ``max_workers=1`` to stay under HuggingFace rate limits.

    Parameters
    ----------
    repo_id : str
        HuggingFace repo, e.g. ``gpahal/bge-m3-onnx-int8``.
    revision : str
        Commit SHA, tag, or branch. Resolved to a SHA for the returned hash.
    dest : Path
        Target directory; created if absent.

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
    logger.info("Downloading %s + %s (serial)", ONNX_FILENAME, TOKENIZER_FILENAME)
    snapshot_download(
        repo_id=repo_id,
        revision=resolved_sha,
        allow_patterns=[ONNX_FILENAME, TOKENIZER_FILENAME],
        local_dir=str(dest),
        max_workers=1,  # serialize file downloads — avoid HF 429 rate limits
    )

    model_path = dest / ONNX_FILENAME
    tokenizer_path = dest / TOKENIZER_FILENAME
    missing = [p.name for p in (model_path, tokenizer_path) if not p.is_file()]
    if missing:
        raise RuntimeError(
            f"download finished but expected files are missing: {missing} "
            f"(in {dest}) — check the repo still ships them under those names"
        )
    return model_path, tokenizer_path, resolved_sha


def main() -> int:
    """Parse args, download the model, and print the ``.env`` lines."""
    parser = argparse.ArgumentParser(
        description="Download the dev BGE-M3 ONNX embedding model from HuggingFace.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"Target directory (default: {DEFAULT_DEST}).",
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"HuggingFace repo id (default: {DEFAULT_REPO_ID}).",
    )
    parser.add_argument(
        "--revision",
        default=DEFAULT_REVISION,
        help="Commit SHA, tag, or branch to pin (default: the vetted commit).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    dest = args.dest.resolve()
    model_path, tokenizer_path, sha = fetch(
        repo_id=args.repo_id, revision=args.revision, dest=dest
    )

    print()
    print("Done. Add these to your .env (or export them), then restart the")
    print("server / Jupyter kernel:")
    print()
    print(f"EMBEDDING_MODEL_PATH={model_path}")
    print(f"EMBEDDING_TOKENIZER_PATH={tokenizer_path}")
    print(f"EMBEDDING_REVISION_HASH={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
