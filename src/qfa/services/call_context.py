"""Request-scoped ContextVars carrying correlation IDs and call context.

Two ContextVars cooperate to unify HTTP-level and service-level correlation:

* ``current_request_id`` — set by the HTTP middleware
  (``RequestIdMiddleware``) for the lifetime of one request, or by a
  non-HTTP caller via :func:`request_id_scope`.
* ``current_call_context`` — set by the orchestrator on each public-method
  entry via :func:`call_scope`. ``TrackingLLMAdapter`` reads it at
  LLM-call time to stamp every persisted ``LLMCallRecord``.

The contract is a single rule: **every** :func:`call_scope` must be
nested inside an active :func:`request_id_scope`. The ``call_id``
stamped onto records is literally ``current_request_id.get()`` — there
is no priority chain, no fallback, no escape hatch. The HTTP
``X-Request-ID``, log lines, and ``llm_calls.call_id`` rows therefore
always share one UUID.

Non-HTTP callers (CLI, future jobs, unit tests) wrap their work in
``request_id_scope(uuid4())``; if they don't, :func:`call_scope` raises
``MissingRequestScopeError`` rather than silently inventing a UUID. A
missing scope is a wiring bug — failing loudly beats producing an
orphan ``call_id`` that joins to nothing.

``asyncio`` propagates ContextVars across ``create_task`` / ``gather`` via
snapshot-on-spawn, so fan-out from a public orchestrator method preserves
both contexts without explicit forwarding.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import UUID

from qfa.domain.errors import MissingRequestScopeError
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
) -> AsyncIterator[CallContext]:
    """Set ``current_call_context`` for the duration of the block.

    The ``call_id`` is always ``current_request_id.get()`` — set by the
    HTTP middleware (``RequestIdMiddleware``) in production, or by the
    caller via :func:`request_id_scope` in tests and non-HTTP entry
    points. If no request scope is active, ``MissingRequestScopeError``
    is raised.

    Parameters
    ----------
    tenant_id : str
        Tenant making the call.
    operation : Operation
        Public orchestrator operation issuing the call.

    Yields
    ------
    CallContext
        The context that was set.

    Raises
    ------
    MissingRequestScopeError
        When no ``request_id_scope`` is active.
    """
    request_id = current_request_id.get()
    if request_id is None:
        raise MissingRequestScopeError(
            "call_scope requires an active request_id_scope. In tests or "
            "non-HTTP entry points, wrap the call in "
            "request_id_scope(uuid4()) first."
        )
    ctx = CallContext(
        tenant_id=tenant_id,
        operation=operation,
        call_id=request_id,
    )
    token = current_call_context.set(ctx)
    try:
        yield ctx
    finally:
        current_call_context.reset(token)
