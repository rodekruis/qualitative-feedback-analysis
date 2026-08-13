"""Composition helpers for constructing the application services.

This module is the *domain-graph* half of the composition root. The
FastAPI lifespan in :mod:`qfa.api.app` still owns *infrastructure*
wiring (database engine, usage repository, ``TrackingLLMAdapter``,
``app.state`` attachment, logging setup) but delegates the construction
of the services themselves — together with their driven adapters
that don't require the database — to this module.

:func:`build_services` returns every application service as a
:class:`ServiceGraph`; :func:`build_orchestrator` is the narrower entry
point for callers that only want the (shrinking) :class:`Orchestrator`.

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

Besides the driven adapters, this module also builds the one
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` every service
delegates its LLM-call scaffolding to. Per ADR-017 that collaborator is
*injected*, not self-constructed, so the composition root stays the one
place where the object graph is assembled — and every service shares a
single instance of it.

The orchestrator may hold *two* LLM connections: the primary one used for
generation, and an optional second one used only for judge calls, configured
via ``JUDGE_LLM_*``. :func:`resolve_judge_llm_settings` applies the
judge/primary inheritance rule here, once, before either client is built.
"""

from __future__ import annotations

import importlib.resources
import logging
from dataclasses import dataclass

import litellm
import yaml

from qfa.adapters.embedding import build_onnx_embedder
from qfa.adapters.presidio_anonymizer import PresidioAnonymizer
from qfa.domain.ports import EmbeddingPort, LLMPort
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.orchestrator import Orchestrator
from qfa.services.sensitivity import SensitivityService
from qfa.settings import AppSettings, EmbeddingSettings, JudgeLLMSettings, LLMSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceGraph:
    """The application services the API publishes on ``app.state``.

    Epic #112 splits ``Orchestrator`` into one service per use case, so the
    composition root now returns more than one object. Grouping them keeps
    the shared parts of the graph — notably the single
    :class:`~qfa.services.llm_call_executor.LLMCallExecutor` — built once,
    and gives the lifespan one thing to construct and unpack. It grows one
    field per extraction and loses ``orchestrator`` when the class is
    deleted (#267).

    Attributes
    ----------
    orchestrator : Orchestrator
        The still-undecomposed use cases (analyze, summarize, coding).
    sensitivity : SensitivityService
        The detect-sensitive use case, extracted in #263.
    """

    orchestrator: Orchestrator
    sensitivity: SensitivityService


def resolve_judge_llm_settings(
    primary: LLMSettings, judge: JudgeLLMSettings
) -> LLMSettings | None:
    """Resolve the judge connection settings against the primary ones.

    This is the *single* place the judge/primary inheritance rule is applied,
    so no ``judge.x or primary.x`` fallback has to be repeated at any call
    site. It runs before either client is built.

    The rule is per field: an explicitly set ``JUDGE_LLM_*`` field overrides
    only itself, every unset field (``None``) keeps the primary's value —
    including ``api_key``, which is why enabling a judge model needs no new
    secret. ``timeout_seconds``, ``max_total_tokens`` and ``chars_per_token``
    have no judge-side override and always come from ``primary``.

    Parameters
    ----------
    primary : LLMSettings
        The primary (generation) LLM connection settings, i.e. ``LLM_*``.
    judge : JudgeLLMSettings
        The judge overrides, i.e. ``JUDGE_LLM_*``.

    Returns
    -------
    LLMSettings | None
        ``None`` when ``judge.model`` is unset or empty — meaning no separate
        judge connection is configured and judge calls should keep using the
        primary client. Otherwise a complete :class:`LLMSettings` describing
        the judge connection, ready to hand to ``build_llm_client``.
    """
    if not judge.model:
        return None

    # Only fields the operator actually set appear in the update, so an unset
    # field falls through to the primary's value untouched.
    overrides = {
        field: value
        for field, value in (
            ("model", judge.model),
            ("api_key", judge.api_key),
            ("api_base", judge.api_base),
            ("api_version", judge.api_version),
        )
        if value is not None
    }
    return primary.model_copy(update=overrides)


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
        A fully-constructed ``OnnxEmbedder`` (for the configured
        ``EMBEDDING_MODEL_KIND``), or ``None`` when ``model_path`` is empty.
    """
    if not settings.model_path:
        logger.info(
            "EMBEDDING_MODEL_PATH not set — hierarchical mode requires it at runtime"
        )
        return None
    return build_onnx_embedder(
        model_kind=settings.model_kind,
        model_path=settings.model_path,
        tokenizer_path=settings.tokenizer_path or settings.model_path,
        revision_hash=settings.revision_hash,
        dense_dim=settings.dense_dim,
        max_tokens=settings.max_tokens,
        intra_op_num_threads=settings.intra_op_num_threads,
        batch_size=settings.batch_size,
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


def build_services(
    settings: AppSettings,
    *,
    llm: LLMPort | None = None,
    judge_llm: LLMPort | None = None,
    embedder: EmbeddingPort | None = None,
) -> ServiceGraph:
    """Construct the application services from application settings.

    This is the shared composition point used by both the FastAPI
    lifespan and out-of-process callers (scripts, notebooks). It owns
    the construction of the services' driven dependencies that do
    not require a database connection: the anonymiser, the LLM client
    (when not overridden), and the optional embedder.

    Every service is built over the *same*
    :class:`~qfa.services.llm_call_executor.LLMCallExecutor`, so the
    per-call timeout, the token ceiling and the anonymiser are configured
    once for the whole graph rather than per use case.

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
    judge_llm : LLMPort | None, optional
        Pre-built LLM port for judge calls, mirroring ``llm``. The FastAPI
        lifespan passes a second ``TrackingLLMAdapter`` here so judge usage
        and cost are recorded too. ``None`` (the default) builds one from
        ``settings.judge_llm`` resolved against ``settings.llm`` — and stays
        ``None`` when ``JUDGE_LLM_MODEL`` is unset, in which case the
        orchestrator runs judge calls on the primary client, exactly as it
        did before the judge connection existed. Note this is resolved
        independently of ``llm``: a caller that injects a fake primary and
        has ``JUDGE_LLM_MODEL`` set in the environment should inject a judge
        fake too, or it will get a real judge client alongside the fake.
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
    ServiceGraph
        The fully wired services, ready to be published on ``app.state``.
    """
    register_custom_model_prices()

    if llm is None:
        # Local import keeps the module free of the FastAPI-specific
        # LLM factory at import time and avoids a circular dependency
        # with qfa.api.app (which imports this module).
        from qfa.api.app import build_llm_client

        llm = build_llm_client(settings.llm)

    if judge_llm is None:
        judge_settings = resolve_judge_llm_settings(settings.llm, settings.judge_llm)
        # None here means no judge model is configured; the orchestrator then
        # routes judge calls to the primary client.
        if judge_settings is not None:
            from qfa.api.app import build_llm_client  # local import, as above

            judge_llm = build_llm_client(judge_settings)

    if embedder is None:
        embedder = build_embedder(settings.embedding)

    anonymizer = PresidioAnonymizer()
    # The shared LLM-call scaffolding is an injected collaborator, not a base
    # class (ADR-017), so the composition root builds it here and hands it to
    # the service rather than letting the service construct its own. It is
    # built over the *primary* LLM connection; judge calls override the client
    # per call.
    executor = LLMCallExecutor(
        llm=llm,
        anonymizer=anonymizer,
        settings=settings.orchestrator,
        llm_timeout_seconds=settings.llm.timeout_seconds,
        max_total_tokens=settings.llm.max_total_tokens,
    )

    return ServiceGraph(
        orchestrator=Orchestrator(
            llm=llm,
            judge_llm=judge_llm,
            anonymizer=anonymizer,
            settings=settings.orchestrator,
            analyze_settings=settings.analyze,
            llm_timeout_seconds=settings.llm.timeout_seconds,
            max_total_tokens=settings.llm.max_total_tokens,
            embedder=embedder,
            executor=executor,
        ),
        sensitivity=SensitivityService(executor=executor),
    )


def build_orchestrator(
    settings: AppSettings,
    *,
    llm: LLMPort | None = None,
    judge_llm: LLMPort | None = None,
    embedder: EmbeddingPort | None = None,
) -> Orchestrator:
    """Construct just the :class:`Orchestrator` from application settings.

    A thin wrapper over :func:`build_services` for the callers that only
    want the orchestrator — scripts, notebooks and evaluation harnesses,
    which run one use case in-process and have no ``app.state`` to publish
    the rest onto. The FastAPI lifespan calls ``build_services`` instead.

    Parameters
    ----------
    settings : AppSettings
        Loaded application settings; see :func:`build_services`.
    llm : LLMPort | None, optional
        Pre-built LLM port; see :func:`build_services`.
    judge_llm : LLMPort | None, optional
        Pre-built judge LLM port; see :func:`build_services`.
    embedder : EmbeddingPort | None, optional
        Pre-built embedder; see :func:`build_services`.

    Returns
    -------
    Orchestrator
        A fully wired orchestrator ready for ``analyze`` /
        ``analyze_hierarchical`` calls.
    """
    return build_services(
        settings, llm=llm, judge_llm=judge_llm, embedder=embedder
    ).orchestrator
