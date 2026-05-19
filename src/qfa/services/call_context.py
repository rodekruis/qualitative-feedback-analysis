"""Request-scoped ContextVars carrying correlation IDs and call context.

Two ContextVars cooperate to unify HTTP-level and service-level correlation:

* ``current_request_id`` — set by the HTTP middleware
  (``RequestIdMiddleware``) for the lifetime of one request. Service-layer
  code never sets it directly.
* ``current_call_context`` — set by the orchestrator on each public-method
  entry via :func:`call_scope`. ``TrackingLLMAdapter`` reads it at
  LLM-call time to stamp every persisted ``LLMCallRecord``.

The two are linked by :func:`call_scope`: when no explicit ``call_id`` is
passed, it adopts ``current_request_id`` so the HTTP ``X-Request-ID``,
log lines, and ``llm_calls.call_id`` rows all share one UUID. Non-HTTP
callers (CLI, future jobs) simply don't enter ``request_id_scope`` — the
ContextVar stays ``None`` and :func:`call_scope` falls back to ``uuid4()``.

``asyncio`` propagates ContextVars across ``create_task`` / ``gather`` via
snapshot-on-spawn, so fan-out from a public orchestrator method preserves
both contexts without explicit forwarding.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import UUID, uuid4

from qfa.domain.models import CallContext, Operation

current_call_context: ContextVar[CallContext | None] = ContextVar(
    "current_call_context",
    default=None,
)

current_request_id: ContextVar[UUID | None] = ContextVar(
    "current_request_id",
    default=None,
)


@asynccontextmanager
async def request_id_scope(request_id: UUID) -> AsyncIterator[UUID]:
    """Set ``current_request_id`` for the lifetime of an HTTP request.

    Entered by the HTTP middleware once per request. :func:`call_scope`
    later adopts the same value as its ``call_id`` so the ID set in the
    ``X-Request-ID`` header reaches the ``llm_calls.call_id`` column
    without explicit threading through method signatures.

    Parameters
    ----------
    request_id : UUID
        The request's correlation ID — also the value placed in the
        ``X-Request-ID`` response header.

    Yields
    ------
    UUID
        The same ``request_id`` for convenience.
    """
    token = current_request_id.set(request_id)
    try:
        yield request_id
    finally:
        current_request_id.reset(token)


@asynccontextmanager
async def call_scope(
    tenant_id: str,
    operation: Operation,
    call_id: UUID | None = None,
) -> AsyncIterator[CallContext]:
    """Set ``current_call_context`` for the duration of the block.

    The ``call_id`` is resolved in priority order:

    1. The explicit ``call_id`` argument, if given.
    2. ``current_request_id`` (set by the HTTP middleware via
       :func:`request_id_scope`), if a request scope is active.
    3. A freshly generated ``uuid4()`` — the fallback for non-HTTP
       callers (CLI, scheduled jobs, tests).

    Parameters
    ----------
    tenant_id : str
        Tenant making the call.
    operation : Operation
        Public orchestrator operation issuing the call.
    call_id : UUID | None
        Optional explicit correlation ID. When ``None`` the ambient
        request ID is adopted; falling back to ``uuid4()`` if absent.

    Yields
    ------
    CallContext
        The context that was set.
    """
    if call_id is not None:
        resolved_call_id = call_id
    else:
        ambient = current_request_id.get()
        resolved_call_id = ambient if ambient is not None else uuid4()
    ctx = CallContext(
        tenant_id=tenant_id,
        operation=operation,
        call_id=resolved_call_id,
    )
    token = current_call_context.set(ctx)
    try:
        yield ctx
    finally:
        current_call_context.reset(token)
