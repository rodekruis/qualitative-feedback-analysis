"""Composition helpers for constructing an :class:`Orchestrator`.

This module is the *domain-graph* half of the composition root. The
FastAPI lifespan in :mod:`qfa.api.app` still owns *infrastructure*
wiring (database engine, usage repository, ``TrackingLLMAdapter``,
``app.state`` attachment, logging setup) but delegates the construction
of the :class:`Orchestrator` itself — together with its driven adapters
that don't require the database — to this module.

Why it lives here rather than at package root:

- ``qfa.api`` already has import-linter permission to import both
  ``qfa.services`` and ``qfa.adapters``; placing the factory here
  keeps the existing contracts untouched.
- ``AGENTS.md`` designates ``qfa.api.app`` as the composition root,
  and this module is a sibling extraction — the architectural role
  hasn't moved, just the construction code.

The factory is intentionally **pure** with respect to the API server's
runtime concerns. It does not construct a database engine, does not
wrap the LLM in :class:`~qfa.adapters.tracking_llm.TrackingLLMAdapter`,
and does not read API keys. Callers that need those concerns
(notably the FastAPI lifespan) build them and pass the wrapped LLM in
via the ``llm`` keyword argument. Callers that don't (scripts,
notebooks, ad-hoc evaluation harnesses) call ``build_orchestrator``
with no overrides and get an Orchestrator over a plain LiteLLM client.
"""

from __future__ import annotations

import importlib.resources
import logging

import litellm
import yaml

from qfa.adapters.embedding import build_bge_m3_embedder
from qfa.adapters.presidio_anonymizer import PresidioAnonymizer
from qfa.domain.ports import EmbeddingPort, LLMPort
from qfa.services.orchestrator import Orchestrator
from qfa.settings import AppSettings, EmbeddingSettings

logger = logging.getLogger(__name__)


def build_embedder(settings: EmbeddingSettings) -> EmbeddingPort | None:
    """Build the self-hosted embedding adapter, or return None when unconfigured.

    The embedder is optional: when ``EMBEDDING_MODEL_PATH`` is not set this
    returns ``None``, and a ``mode=hierarchical`` request then fails with
    502 ``analysis_unavailable`` (the orchestrator raises ``AnalysisError``
    when its embedder is ``None``); ``single_pass`` is unaffected.
    Production deployments set the path variables; local / CI runs omit them
    so the normal test suite never downloads a multi-GB model.

    Parameters
    ----------
    settings : EmbeddingSettings
        Embedding configuration loaded from environment variables.

    Returns
    -------
    EmbeddingPort | None
        A fully-constructed ``BgeM3OnnxEmbedder``, or ``None`` when
        ``model_path`` is empty.
    """
    if not settings.model_path:
        logger.info(
            "EMBEDDING_MODEL_PATH not set — hierarchical mode requires it at runtime"
        )
        return None
    return build_bge_m3_embedder(
        model_path=settings.model_path,
        tokenizer_path=settings.tokenizer_path or settings.model_path,
        revision_hash=settings.revision_hash,
        intra_op_num_threads=settings.intra_op_num_threads,
    )


def register_custom_model_prices() -> None:
    """Load custom model pricing from the bundled YAML resource.

    Registers models with LiteLLM so that ``completion_cost()`` works
    for models not in the built-in cost map. Idempotent: LiteLLM's
    ``register_model`` overwrites existing entries with the same key,
    so repeated calls (e.g. once per ``build_orchestrator`` in a
    notebook) are safe.
    """
    prices_path = importlib.resources.files("qfa.resources").joinpath(
        "model_prices.yaml"
    )
    with importlib.resources.as_file(prices_path) as f:
        custom_prices = yaml.safe_load(f.read_text())
    if custom_prices and custom_prices.get("models"):
        litellm.register_model(custom_prices["models"])
        logger.info(
            "Registered %d custom model price(s) for %s",
            len(custom_prices["models"]),
            list(custom_prices["models"].keys()),
        )


def build_orchestrator(
    settings: AppSettings,
    *,
    llm: LLMPort | None = None,
    embedder: EmbeddingPort | None = None,
) -> Orchestrator:
    """Construct an :class:`Orchestrator` from application settings.

    This is the shared composition point used by both the FastAPI
    lifespan and out-of-process callers (scripts, notebooks). It owns
    the construction of the orchestrator's driven dependencies that do
    not require a database connection: the anonymiser, the LLM client
    (when not overridden), and the optional embedder.

    Parameters
    ----------
    settings : AppSettings
        Loaded application settings. Sub-settings consulted:
        ``llm`` (for the default LLM client), ``embedding`` (for the
        default embedder), ``orchestrator``, and ``analyze``.
    llm : LLMPort | None, optional
        Pre-built LLM port to use instead of constructing one from
        ``settings.llm``. The FastAPI lifespan passes a
        :class:`~qfa.adapters.tracking_llm.TrackingLLMAdapter` here so
        usage is recorded; scripts can pass a logging wrapper or a fake
        for offline runs. ``None`` (the default) builds a plain
        ``LiteLLMClient`` — suitable for one-shot scripts that don't
        need DB-backed tracking.
    embedder : EmbeddingPort | None, optional
        Pre-built embedder to use instead of constructing one from
        ``settings.embedding``. Pass an explicit value when the caller
        has already constructed one (e.g. the lifespan, which logs its
        construction before delegating). ``None`` (the default) builds
        one via :func:`build_embedder` and may legitimately remain
        ``None`` when the embedding model path is unset — in that case
        hierarchical analysis will fail at runtime with ``AnalysisError``
        (single-pass remains usable).

    Returns
    -------
    Orchestrator
        A fully wired orchestrator ready for ``analyze`` /
        ``analyze_hierarchical`` calls.
    """
    register_custom_model_prices()

    if llm is None:
        # Local import keeps the module free of the FastAPI-specific
        # LLM factory at import time and avoids a circular dependency
        # with qfa.api.app (which imports this module).
        from qfa.api.app import build_llm_client

        llm = build_llm_client(settings.llm)

    if embedder is None:
        embedder = build_embedder(settings.embedding)

    return Orchestrator(
        llm=llm,
        anonymizer=PresidioAnonymizer(),
        settings=settings.orchestrator,
        analyze_settings=settings.analyze,
        llm_timeout_seconds=settings.llm.timeout_seconds,
        max_total_tokens=settings.llm.max_total_tokens,
        embedder=embedder,
    )
