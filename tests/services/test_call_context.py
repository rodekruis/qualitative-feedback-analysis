"""Tests for the request-scoped call-context ContextVar."""

import asyncio
from uuid import uuid4

import pytest

from qfa.domain.errors import MissingRequestScopeError
from qfa.domain.models import Operation
from qfa.services.call_context import (
    call_scope,
    current_call_context,
    current_request_id,
    request_id_scope,
)

pytestmark = pytest.mark.asyncio


async def test_current_call_context_is_none_outside_scope():
    """No ``call_scope`` has been entered → ``current_call_context`` is None.

    Guards the default state so a stray ``current_call_context.get()``
    can't accidentally see a leaked context from a prior request.
    """
    assert current_call_context.get() is None


async def test_call_scope_sets_and_resets_inside_request_scope():
    """``call_scope`` populates ``current_call_context`` for the block and unsets on exit.

    Mirrors the production path: HTTP middleware sets a request scope,
    orchestrator enters a call scope, both unwind in reverse on exit.
    """
    async with request_id_scope(uuid4()):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
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

    async with request_id_scope(uuid4()):
        async with call_scope(tenant_id="t1", operation=Operation.SUMMARIZE):
            await asyncio.create_task(reader())

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].tenant_id == "t1"
    assert captured[0].operation == Operation.SUMMARIZE


async def test_current_request_id_is_none_outside_scope():
    """No ``request_id_scope`` has been entered → ``current_request_id`` is None.

    Non-HTTP entry points (CLI, future jobs) never call the middleware,
    so the ContextVar must default to None — exposing the missing scope
    via the explicit ``MissingRequestScopeError`` rather than masking it.
    """
    assert current_request_id.get() is None


async def test_request_id_scope_sets_and_resets():
    """``request_id_scope`` populates ``current_request_id`` and unsets on exit.

    Mirrors the lifecycle of ``call_scope`` — set on enter, reset on
    exit — so successive requests in one event loop don't leak.
    """
    fixed = uuid4()
    async with request_id_scope(fixed):
        assert current_request_id.get() == fixed
    assert current_request_id.get() is None


async def test_call_scope_inherits_request_id_as_call_id():
    """Inside ``request_id_scope``, ``call_scope`` uses the same UUID as call_id.

    This is the unification contract: one UUID set by HTTP middleware
    becomes the same ID stamped onto every LLM call record persisted
    inside the request — joinable across header, logs, and DB.
    """
    fixed = uuid4()
    async with request_id_scope(fixed):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx:
            assert ctx.call_id == fixed


async def test_nested_request_id_scope_wins_over_outer():
    """A nested ``request_id_scope`` overrides the outer one for its body.

    The pinning mechanism for tests / non-HTTP callers: re-enter
    ``request_id_scope`` with a fixed UUID to control what ``call_scope``
    sees, without having to add an escape-hatch parameter to call_scope.
    """
    outer = uuid4()
    inner = uuid4()
    async with request_id_scope(outer):
        async with request_id_scope(inner):
            async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx:
                assert ctx.call_id == inner
        # Outer scope restored on inner exit.
        assert current_request_id.get() == outer


async def test_call_scope_without_request_scope_raises():
    """Entering ``call_scope`` outside any ``request_id_scope`` raises.

    The collapsed contract: every call_scope must be nested inside a
    request scope. A missing scope is a wiring bug — silently generating
    a UUID would produce an orphan call_id that joins to nothing.
    """
    with pytest.raises(MissingRequestScopeError):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
            pass


async def test_separate_request_scopes_produce_distinct_call_ids():
    """Different ``request_id_scope`` enters yield distinct ``call_id`` values.

    Two API invocations must not collide on the correlation key,
    otherwise per-invocation aggregation in ``/v1/usage`` would silently
    merge them.
    """
    async with request_id_scope(uuid4()):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx1:
            first = ctx1.call_id
    async with request_id_scope(uuid4()):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx2:
            second = ctx2.call_id
    assert first != second
