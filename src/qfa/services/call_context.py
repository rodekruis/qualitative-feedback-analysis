"""Request-scoped ContextVar carrying tenant + operation to tracking adapters.

The orchestrator enters ``call_scope(...)`` at each public-method entry; the
``TrackingLLMAdapter`` reads ``current_call_context.get()`` at LLM-call time.
``asyncio`` propagates ContextVars across ``create_task`` / ``gather`` via
snapshot-on-spawn, so fan-out from a public orchestrator method preserves the
context without explicit forwarding.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from qfa.domain.models import CallContext, Operation

current_call_context: ContextVar[CallContext | None] = ContextVar(
    "current_call_context",
    default=None,
)


@asynccontextmanager
async def call_scope(
    tenant_id: str,
    operation: Operation,
) -> AsyncIterator[CallContext]:
    """Set ``current_call_context`` for the duration of the block.

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
    """
    ctx = CallContext(tenant_id=tenant_id, operation=operation)
    token = current_call_context.set(ctx)
    try:
        yield ctx
    finally:
        current_call_context.reset(token)
