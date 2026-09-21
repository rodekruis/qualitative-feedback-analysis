"""Request-scoped ContextVar carrying the active call's correlation context.

A single ``ContextVar[CallContext | None]`` propagates ``tenant_id``,
``operation``, and ``call_id`` from the driving adapter (the FastAPI
dependency :func:`~qfa.api.dependencies.call_scope_for` in production)
down to the driven adapter (``TrackingLLMAdapter``), which reads it
when stamping each persisted ``LLMCallRecord``. The orchestrator in
between never touches it.

Entry is via :func:`call_scope`, which takes the correlation UUID as a
required argument — in HTTP requests, that's the same UUID
``RequestIdMiddleware`` placed in ``X-Request-ID``, so header, logs,
and ``llm_calls.call_id`` rows always join cleanly. Non-HTTP callers
(CLI, jobs, tests) pass ``request_id=uuid4()`` themselves.

``asyncio`` propagates ContextVars across ``create_task`` / ``gather``
via snapshot-on-spawn, so fan-out from a public orchestrator method
preserves the context without explicit forwarding. :func:`call_scope`
also attaches an OpenTelemetry context for the same reason (#354): every
LLM-call span ``LiteLLMClient.complete`` opens inside the block —
generation and judge calls alike — inherits ``request_id`` as its trace
id (see :func:`_otel_context_for`), so they land in Langfuse as one
trace per request instead of one trace per LLM call, and a live judge
score sent against ``call_id.hex`` joins that trace.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from uuid import UUID

from opentelemetry import context as otel_context
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace import set_span_in_context as otel_set_span_in_context

from qfa.domain.usage_models import CallContext, Operation

_SPAN_ID_MASK = (1 << 64) - 1


def _otel_context_for(call_id: UUID) -> otel_context.Context:
    """Build an OTel context whose current span's trace id is ``call_id``.

    The span is a :class:`NonRecordingSpan` — a placeholder used only to
    propagate a trace id via the ambient OTel context, never exported
    itself. Every real span opened underneath it (``Tracer.start_as_current_span``
    with no explicit ``context=``, which is how ``LiteLLMClient.complete``
    calls it) inherits this trace id as a child. ``span_id`` is derived
    from ``call_id`` rather than randomly generated: it is never recorded
    or read back, so determinism costs nothing and avoids pulling in an
    ID generator for a throwaway value.
    """
    span_context = SpanContext(
        trace_id=call_id.int,
        span_id=call_id.int & _SPAN_ID_MASK,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return otel_set_span_in_context(NonRecordingSpan(span_context))


current_call_context: ContextVar[CallContext | None] = ContextVar(
    "current_call_context",
    default=None,
)

current_call_role: ContextVar[str] = ContextVar(
    "current_call_role", default="generation"
)
"""Whether the LLM call in flight is a judge call or a generation call.

Read by ``LiteLLMClient`` to name/tag its Langfuse span -- a dimension
``current_call_context`` cannot carry, since one ``call_scope`` (one
orchestrator operation) makes calls of *both* roles (e.g. hierarchical
analyze's map/reduce generation calls and its leaf-judge calls). Not read
by ``TrackingLLMAdapter``: ``LLMCallRecord`` has no such column, so this
role is currently a Langfuse-only, not a Postgres-usage, dimension.
"""


@contextmanager
def judge_call() -> Iterator[None]:
    """Mark the LLM calls made inside this block as judge calls.

    Wrap a judge call site in ``with judge_call():`` — every current site
    already knows it is calling the judge (it is calling ``self._judge_llm``,
    or passing ``llm=self._judge_llm`` to the executor), this just makes that
    fact visible to ``LiteLLMClient`` too. Synchronous (no ``async with``
    needed): setting/resetting a ``ContextVar`` does no I/O, only the
    caller's own ``await`` inside the block does.
    """
    token = current_call_role.set("judge")
    try:
        yield
    finally:
        current_call_role.reset(token)


@asynccontextmanager
async def call_scope(
    tenant_id: str,
    operation: Operation,
    request_id: UUID,
) -> AsyncIterator[CallContext]:
    """Set ``current_call_context`` for the duration of the block.

    Parameters
    ----------
    tenant_id : str
        Tenant making the call.
    operation : Operation
        Public orchestrator operation issuing the call.
    request_id : UUID
        Correlation UUID for the API invocation. Becomes the
        ``call_id`` field of the resulting ``CallContext`` and is
        stamped onto every ``LLMCallRecord`` persisted inside the
        scope. In HTTP requests this is the same UUID set in the
        ``X-Request-ID`` header by ``RequestIdMiddleware``; non-HTTP
        callers pass a freshly-generated ``uuid4()``.

    Yields
    ------
    CallContext
        The context that was set.
    """
    ctx = CallContext(
        tenant_id=tenant_id,
        operation=operation,
        call_id=request_id,
    )
    token = current_call_context.set(ctx)
    otel_token = otel_context.attach(_otel_context_for(request_id))
    try:
        yield ctx
    finally:
        otel_context.detach(otel_token)
        current_call_context.reset(token)
