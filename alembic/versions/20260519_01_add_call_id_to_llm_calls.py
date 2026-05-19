"""Add call_id to llm_calls for per-invocation aggregation.

Revision ID: 20260519_01
Revises: 20260429_01
Create Date: 2026-05-19

Adds a ``call_id`` UUID column linking all LLM calls made within a single
API invocation (one entry of ``call_scope`` in the orchestrator). Enables
aggregations of cost / duration / tokens per API call (e.g., average cost
per ``analyze`` invocation across its fan-out of LLM calls).

Backfill: each existing row receives its own distinct UUID via
``gen_random_uuid()`` (Postgres built-in, no extension required). Old
rows therefore look like one-LLM-call invocations, which is an honest
representation of pre-feature data and keeps downstream aggregation
queries free of NULL handling.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260519_01"
down_revision: Union[str, Sequence[str], None] = "20260429_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``call_id`` column, backfill with synthetic UUIDs, then add index."""
    op.add_column(
        "llm_calls",
        sa.Column("call_id", sa.Uuid(), nullable=True),
    )
    op.execute("UPDATE llm_calls SET call_id = gen_random_uuid() WHERE call_id IS NULL")
    op.alter_column("llm_calls", "call_id", nullable=False)
    op.create_index(
        "idx_llm_calls_tenant_operation_call_id",
        "llm_calls",
        ["tenant_id", "operation", "call_id"],
    )


def downgrade() -> None:
    """Drop the index and the ``call_id`` column."""
    op.drop_index("idx_llm_calls_tenant_operation_call_id", table_name="llm_calls")
    op.drop_column("llm_calls", "call_id")
