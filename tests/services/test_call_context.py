"""Tests for the request-scoped call-context ContextVar."""

import asyncio
from uuid import UUID, uuid4

import pytest

from qfa.domain.models import Operation
from qfa.services.call_context import call_scope, current_call_context

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
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx:
        assert isinstance(ctx.call_id, UUID)


async def test_call_scope_generates_distinct_call_ids_for_separate_scopes():
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx1:
        first = ctx1.call_id
    async with call_scope(tenant_id="t1", operation=Operation.ANALYZE) as ctx2:
        second = ctx2.call_id
    assert first != second


async def test_call_scope_accepts_explicit_call_id_override():
    fixed = uuid4()
    async with call_scope(
        tenant_id="t1", operation=Operation.ANALYZE, call_id=fixed
    ) as ctx:
        assert ctx.call_id == fixed
        from_var = current_call_context.get()
        assert from_var is not None
        assert from_var.call_id == fixed
