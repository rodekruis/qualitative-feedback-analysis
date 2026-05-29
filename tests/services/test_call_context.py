"""Tests for the request-scoped call-context ContextVar."""

import asyncio
from uuid import uuid4

import pytest

from qfa.domain.usage_models import Operation
from qfa.services.call_context import call_scope, current_call_context

pytestmark = pytest.mark.asyncio


async def test_current_call_context_is_none_outside_scope():
    """No ``call_scope`` has been entered → ``current_call_context`` is None.

    Guards the default state so a stray ``current_call_context.get()``
    can't accidentally see a leaked context from a prior request.
    """
    assert current_call_context.get() is None


async def test_call_scope_sets_and_resets():
    """``call_scope`` populates ``current_call_context`` for the block and unsets on exit.

    Mirrors the production path: the FastAPI dependency enters
    ``call_scope`` once per request; the ContextVar is then visible to
    every downstream coroutine and reset on the way out.
    """
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, request_id=uuid4()
    ):
        ctx = current_call_context.get()
        assert ctx is not None
        assert ctx.tenant_id == "t1"
        assert ctx.operation == Operation.ANALYZE
    assert current_call_context.get() is None


async def test_call_scope_propagates_through_create_task():
    """A child task spawned inside ``call_scope`` sees the same context.

    Confirms the asyncio ContextVar snapshot-on-spawn behavior the
    orchestrator's fan-out (``analyze``, ``summarize_aggregate``) relies on.
    """
    captured: list = []

    async def reader() -> None:
        captured.append(current_call_context.get())

    async with call_scope(
        tenant_id="t1", operation=Operation.SUMMARIZE, request_id=uuid4()
    ):
        await asyncio.create_task(reader())

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].tenant_id == "t1"
    assert captured[0].operation == Operation.SUMMARIZE


async def test_call_scope_stamps_request_id_as_call_id():
    """The ``request_id`` argument becomes the ``call_id`` of the resulting context.

    This is the unification contract: the UUID set by HTTP middleware
    in ``X-Request-ID`` is passed verbatim to ``call_scope``, becomes
    ``ctx.call_id``, and is stamped onto every persisted
    ``LLMCallRecord``. Header, logs, and DB row all join cleanly.
    """
    fixed = uuid4()
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, request_id=fixed
    ) as ctx:
        assert ctx.call_id == fixed


async def test_nested_call_scope_wins_over_outer():
    """A nested ``call_scope`` overrides the outer one for its body.

    Successive ``call_scope`` enters compose via the ContextVar's
    token/reset semantics — useful when a test wants to control the
    correlation ID for an inner block while keeping a surrounding scope
    untouched.
    """
    outer = uuid4()
    inner = uuid4()
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, request_id=outer
    ) as outer_ctx:
        async with call_scope(
            tenant_id="t1", operation=Operation.ANALYZE, request_id=inner
        ) as inner_ctx:
            assert current_call_context.get() is inner_ctx
            assert inner_ctx.call_id == inner
        # Outer scope restored on inner exit.
        assert current_call_context.get() is outer_ctx
        assert outer_ctx.call_id == outer


async def test_separate_call_scopes_produce_distinct_call_ids():
    """Different ``call_scope`` enters yield distinct ``call_id`` values.

    Two API invocations must not collide on the correlation key,
    otherwise per-invocation aggregation in ``/v1/usage`` would silently
    merge them.
    """
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, request_id=uuid4()
    ) as ctx1:
        first = ctx1.call_id
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, request_id=uuid4()
    ) as ctx2:
        second = ctx2.call_id
    assert first != second
