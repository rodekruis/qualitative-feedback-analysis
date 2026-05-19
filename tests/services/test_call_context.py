"""Tests for the request-scoped call-context ContextVar."""

import asyncio
from uuid import UUID, uuid4

import pytest

from qfa.domain.models import Operation
from qfa.services.call_context import (
    call_scope,
    current_call_context,
    current_request_id,
    request_id_scope,
)

pytestmark = pytest.mark.asyncio


async def test_current_call_context_is_none_outside_scope():
    assert current_call_context.get() is None


async def test_call_scope_sets_and_resets():
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE):
        ctx = current_call_context.get()
        assert ctx is not None
        assert ctx.tenant_id == "t1"
        assert ctx.operation == Operation.ANALYZE
    assert current_call_context.get() is None


async def test_call_scope_propagates_through_create_task():
    captured: list = []

    async def reader() -> None:
        captured.append(current_call_context.get())

    async with call_scope(tenant_id="t1", operation=Operation.SUMMARIZE):
        await asyncio.create_task(reader())

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].tenant_id == "t1"
    assert captured[0].operation == Operation.SUMMARIZE


async def test_call_scope_auto_generates_call_id():
    """``call_scope`` must populate ``ctx.call_id`` with a UUID by default.

    Callers shouldn't be required to supply a ``call_id`` — the scope should
    mint one so the orchestrator doesn't need to plumb correlation IDs.
    """
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx:
        assert isinstance(ctx.call_id, UUID)


async def test_call_scope_generates_distinct_call_ids_for_separate_scopes():
    """Separate ``call_scope`` enters must produce different ``call_id`` values.

    Two API invocations must not collide on the correlation key, otherwise
    per-invocation aggregation in ``/v1/usage`` would silently merge them.
    """
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx1:
        first = ctx1.call_id
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx2:
        second = ctx2.call_id
    assert first != second


async def test_current_request_id_is_none_outside_scope():
    """No ``request_id_scope`` has been entered → ``current_request_id`` is None.

    Non-HTTP entry points (CLI, future jobs) never call the middleware,
    so the ContextVar must default to None for ``call_scope`` to fall
    back to ``uuid4()``.
    """
    assert current_request_id.get() is None


async def test_request_id_scope_sets_and_resets():
    """``request_id_scope`` populates ``current_request_id`` and unsets on exit.

    Mirrors the lifecycle of the existing ``call_scope`` — set on enter,
    reset on exit — so successive requests in one event loop don't leak.
    """
    fixed = uuid4()
    async with request_id_scope(fixed):
        assert current_request_id.get() == fixed
    assert current_request_id.get() is None


async def test_call_scope_inherits_request_id_when_no_explicit_call_id():
    """When inside ``request_id_scope``, ``call_scope`` uses the same UUID as call_id.

    This is the unification contract: one UUID set by HTTP middleware
    becomes the same ID stamped onto every LLM call record persisted
    inside the request — joinable across header, logs, and DB.
    """
    fixed = uuid4()
    async with request_id_scope(fixed):
        async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx:
            assert ctx.call_id == fixed


async def test_call_scope_explicit_call_id_wins_over_request_id():
    """An explicit ``call_id`` argument overrides the ambient ``request_id``.

    Lets tests pin the value without unwinding the middleware, and lets
    non-HTTP callers explicitly choose a correlation ID inside a request
    scope if they really want to.
    """
    request_uuid = uuid4()
    pinned = uuid4()
    async with request_id_scope(request_uuid):
        async with call_scope(
            tenant_id="t1", operation=Operation.ANALYZE, call_id=pinned
        ) as ctx:
            assert ctx.call_id == pinned


async def test_call_scope_falls_back_to_uuid4_when_no_request_id():
    """Outside ``request_id_scope``, ``call_scope`` generates a fresh UUID.

    The non-HTTP fallback path — guards that removing the middleware (or
    running from the CLI/tests) still produces a valid, unique call_id.
    """
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx:
        assert isinstance(ctx.call_id, UUID)


async def test_call_scope_accepts_explicit_call_id_override():
    """An explicit ``call_id`` argument must override auto-generation.

    Needed so a future request-ID middleware (or tests) can pin the
    correlation ID to a known value rather than the auto-generated UUID.
    """
    fixed = uuid4()
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, call_id=fixed
    ) as ctx:
        assert ctx.call_id == fixed
        from_var = current_call_context.get()
        assert from_var is not None
        assert from_var.call_id == fixed
