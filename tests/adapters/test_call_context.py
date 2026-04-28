"""Tests for the request-scoped call-context ContextVar."""

import asyncio

import pytest

from qfa.adapters.call_context import call_scope, current_call_context
from qfa.domain.models import Operation

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
