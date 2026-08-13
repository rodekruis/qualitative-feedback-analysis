"""Composition helpers for constructing the application services.

This module is the *domain-graph* half of the composition root. The
FastAPI lifespan in :mod:`qfa.api.app` still owns *infrastructure*
wiring (database engine, usage repository, ``TrackingLLMAdapter``,
``app.state`` attachment, logging setup) but delegates the construction
of the application services themselves — together with their driven
adapters that don't require the database — to this module.

:func:`build_service_graph` returns every application service as a
:class:`ServiceGraph`; :func:`build_orchestrator` is the narrower entry
point for callers that only want the (shrinking) :class:`Orchestrator`.

Why it lives here rather than at package root:

- ``qfa.api`` already has import-linter permission to import both
  ``qfa.services`` and ``qfa.adapters``; placing the factory here
  keeps the existing contracts untouched.
- ``AGENTS.md`` designates ``qfa.api.app`` as the composition root,
  and this module is a sibling extraction — the architectural role
  hasn't moved, just the construction code.

The factories are intentionally **pure** with respect to the API server's
runtime concerns. They do not construct a database engine, do not
wrap the LLM in :class:`~qfa.adapters.tracking_llm.TrackingLLMAdapter`,
and do not read API keys. Callers that need those concerns
(notably the FastAPI lifespan) build them and pass the wrapped LLM in
via the ``llm`` keyword argument. Callers that don't (scripts,
notebooks, ad-hoc evaluation harnesses) call ``build_service_graph`` (or one
of its single-service wrappers) with no overrides and get services over
a plain LiteLLM client.

Besides the driven adapters, this module also builds the
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` the services
delegate their LLM-call scaffolding to. Per ADR-017 that collaborator is
*injected*, not self-constructed, so the composition root stays the one
place where the object graph is assembled — and there is exactly **one**
executor instance, shared by every service in the graph.

Epic #112 is extracting one service per use case out of ``Orchestrator``.
While that is in flight the graph holds both the shrinking ``Orchestrator``
and the already-extracted services, so :func:`build_service_graph` returns a
:class:`ServiceGraph` rather than a single object. :func:`build_orchestrator`
remains as the narrow entry point for callers that only want the
orchestrator (scripts, notebooks, evaluation harnesses).

The services may hold *two* LLM connections: the primary one used for
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
from qfa.services.analyze import AnalyzeService
from qfa.services.coding import CodingService
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.orchestrator import Orchestrator
from qfa.services.sensitivity import SensitivityService
from qfa.services.summarize import SummarizeService
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
        The now fully-decomposed use cases; the class itself is deleted
        in #267.
    sensitivity : SensitivityService
        The detect-sensitive use case, extracted in #263.
    coding : CodingService
        The assign-codes use case, backing ``POST /v1/assign-codes``.
    analyze : AnalyzeService
        The analyze use case (analyze_bulk, analyze_hierarchical),
        extracted in #266.
    summarize : SummarizeService
        The summarize / summarize_bulk use cases, extracted in #264.
    """

    orchestrator: Orchestrator
    sensitivity: SensitivityService
    coding: CodingService
    analyze: AnalyzeService
    summarize: SummarizeService


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
    502 ``analysis_unavailable`` (:class:`~qfa.services.analyze.AnalyzeService`
    raises ``AnalysisError`` when its embedder is ``None``); ``single_pass``
    is unaffected.
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


def build_service_graph(
    settings: AppSettings,
    *,
    llm: LLMPort | None = None,
    judge_llm: LLMPort | None = None,
    embedder: EmbeddingPort | None = None,
) -> ServiceGraph:
    """Construct every application service from application settings.

    This is the shared composition point used by both the FastAPI
    lifespan and out-of-process callers (scripts, notebooks). It owns
    the construction of the services' driven dependencies that do
    not require a database connection: the anonymiser, the LLM client
    (when not overridden), and the optional embedder — plus the one
    :class:`LLMCallExecutor` every service shares.

    Every service is built over the *same*
    :class:`~qfa.services.llm_call_executor.LLMCallExecutor`, so the
    per-call timeout, the token ceiling and the anonymiser are configured
    once for the whole graph rather than per use case.

    Callers that want only the orchestrator can use the narrower
    :func:`build_orchestrator` instead; it delegates here.

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
        services run judge calls on the primary client, exactly as they
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
        (single-pass remains usable). Only :class:`AnalyzeService` takes
        it; no other use case needs an embedder.

    Returns
    -------
    ServiceGraph
        Every application service, fully wired over one shared executor and
        one shared anonymiser, ready to be published on ``app.state``.
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
        # None here means no judge model is configured; the services then
        # route judge calls to the primary client.
        if judge_settings is not None:
            from qfa.api.app import build_llm_client  # local import, as above

            judge_llm = build_llm_client(judge_settings)

    if embedder is None:
        embedder = build_embedder(settings.embedding)

    anonymizer = PresidioAnonymizer()
    # The shared LLM-call scaffolding is an injected collaborator, not a base
    # class (ADR-017), so the composition root builds it here — once — and
    # hands the same instance to every service rather than letting each
    # service construct its own. It is built over the *primary* LLM
    # connection; judge calls override the client per call.
    executor = LLMCallExecutor(
        llm=llm,
        anonymizer=anonymizer,
        settings=settings.orchestrator,
        llm_timeout_seconds=settings.llm.timeout_seconds,
        max_total_tokens=settings.llm.max_total_tokens,
    )

    orchestrator = Orchestrator(
        llm=llm,
        judge_llm=judge_llm,
        anonymizer=anonymizer,
        settings=settings.orchestrator,
        llm_timeout_seconds=settings.llm.timeout_seconds,
        max_total_tokens=settings.llm.max_total_tokens,
        executor=executor,
    )

    analyze = AnalyzeService(
        executor=executor,
        llm=llm,
        judge_llm=judge_llm,
        anonymizer=anonymizer,
        settings=settings.orchestrator,
        analyze_settings=settings.analyze,
        max_total_tokens=settings.llm.max_total_tokens,
        embedder=embedder,
    )

    return ServiceGraph(
        orchestrator=orchestrator,
        sensitivity=SensitivityService(executor=executor),
        # No judge client: both the pick and the per-level judge in the coding
        # path run on the primary connection (#258 scoped the split to analyse
        # and summarise), so the service is never handed one.
        coding=CodingService(llm=llm, anonymizer=anonymizer, executor=executor),
        analyze=analyze,
        # Neither summarisation path runs the token-budget guard or needs an
        # embedder, so the service asks for neither — the constructor is the
        # use cases' real dependency surface (ADR-017, option A).
        summarize=SummarizeService(
            llm=llm,
            judge_llm=judge_llm,
            anonymizer=anonymizer,
            executor=executor,
        ),
    )


def build_orchestrator(
    settings: AppSettings,
    *,
    llm: LLMPort | None = None,
    judge_llm: LLMPort | None = None,
) -> Orchestrator:
    """Build only the :class:`Orchestrator` half of :func:`build_service_graph`.

    Convenience wrapper for callers that need just the (now empty)
    :class:`Orchestrator` shell — scripts, notebooks and ad-hoc evaluation
    harnesses written against it before #267 deletes the class. There is
    no ``embedder`` parameter: the orchestrator no longer holds one, so
    accepting it here would be a silent no-op.

    Parameters
    ----------
    settings : AppSettings
        Loaded application settings; see :func:`build_service_graph`.
    llm : LLMPort | None, optional
        Pre-built primary LLM port, or ``None`` to build one from settings.
    judge_llm : LLMPort | None, optional
        Pre-built judge LLM port, or ``None`` to resolve one from settings.

    Returns
    -------
    Orchestrator
        The orchestrator shell, kept until :class:`Orchestrator` itself is
        deleted in #267.
    """
    return build_service_graph(settings, llm=llm, judge_llm=judge_llm).orchestrator


def build_analyze_service(
    settings: AppSettings,
    *,
    llm: LLMPort | None = None,
    judge_llm: LLMPort | None = None,
    embedder: EmbeddingPort | None = None,
) -> AnalyzeService:
    """Build only the :class:`AnalyzeService` half of :func:`build_service_graph`.

    Convenience wrapper for scripts and notebooks that drive
    ``analyze_bulk`` / ``analyze_hierarchical`` in-process. Pass
    ``embedder`` (or configure ``EMBEDDING_MODEL_PATH``) for the
    hierarchical mode; without one it raises ``AnalysisError`` at request
    time and ``single_pass`` still works.
    """
    return build_service_graph(
        settings, llm=llm, judge_llm=judge_llm, embedder=embedder
    ).analyze
