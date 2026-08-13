"""Orchestrator service — core business logic for feedback analysis.

The scaffolding it once used to assemble prompts, enforce token limits,
filter prompt injection, manage retries and enforce deadlines lives on the
injected :class:`~qfa.services.llm_call_executor.LLMCallExecutor` rather
than on this class (ADR-017).

Every use case has now been extracted out of this class into its own
service — :class:`~qfa.services.sensitivity.SensitivityService` (#263),
:class:`~qfa.services.coding.CodingService` (#265),
:class:`~qfa.services.analyze.AnalyzeService` (#266) and
:class:`~qfa.services.summarize.SummarizeService` (#264). What remains here
is the constructor alone, kept only because the composition root
(:func:`qfa.api.composition.build_service_graph`) still builds one; #267
deletes the class entirely.
"""

from qfa.domain.ports import AnonymizationPort, LLMPort
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.settings import OrchestratorSettings


class Orchestrator:
    """Vestigial orchestration service — every use case has moved off it.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter. Unused directly now that every use case
        has moved to its own service; kept only because the constructor
        is still called from the composition root.
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before LLM calls.
    settings : OrchestratorSettings
        Cross-cutting orchestrator configuration (retry policy, token
        budget estimation, metadata allow-list).
    llm_timeout_seconds : float
        Maximum time in seconds for a single LLM call.
    max_total_tokens : int
        Maximum estimated total tokens for a single request.
    judge_llm : LLMPort | None
        Optional separate adapter for the LLM-as-judge quality-score calls.
        No call site on this class uses it any more: the last two — the
        judges in ``summarize_bulk`` and ``summarize`` — moved to
        :class:`~qfa.services.summarize.SummarizeService` in #264. Kept on
        the constructor only because the composition root still passes
        one; #267 deletes the class and this parameter with it.
    executor : LLMCallExecutor | None
        The shared LLM-call scaffolding (anonymise-records, deadline→timeout
        derivation, token-budget guard, semaphore-bounded completion),
        per ADR-017. The composition root
        (:func:`qfa.api.composition.build_service_graph`) constructs it
        explicitly, and hands the *same* instance to every other service.
        ``None`` (the default) builds one over the same ``llm``,
        ``anonymizer``, ``settings``, ``llm_timeout_seconds`` and
        ``max_total_tokens`` this constructor already received, so callers
        that don't care about the collaborator — scripts, notebooks, and the
        bulk of the test suite — need not thread it through.
    """

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        settings: OrchestratorSettings,
        llm_timeout_seconds: float,
        max_total_tokens: int,
        judge_llm: LLMPort | None = None,
        executor: LLMCallExecutor | None = None,
    ) -> None:
        self._llm = llm
        # Judge calls run on their own connection when one is configured, so
        # the generator does not grade its own output. Falling back to the
        # primary client keeps the default (no JUDGE_LLM_MODEL) behaviour
        # identical to before the judge connection existed, and means call
        # sites never branch — they just use _judge_llm.
        self._judge_llm = judge_llm if judge_llm is not None else llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._settings = settings
        self._llm_timeout_seconds = llm_timeout_seconds
        self._max_total_tokens = max_total_tokens
        # Shared LLM-call scaffolding (ADR-017): an injected collaborator, not a
        # base class. Default-constructed from the arguments above so the
        # composition root can inject one without every other construction site
        # having to. Either way it is built over the *primary* llm; judge calls
        # pass ``llm=self._judge_llm`` per call.
        self._executor = executor or LLMCallExecutor(
            llm=llm,
            anonymizer=anonymizer,
            settings=settings,
            llm_timeout_seconds=llm_timeout_seconds,
            max_total_tokens=max_total_tokens,
        )
