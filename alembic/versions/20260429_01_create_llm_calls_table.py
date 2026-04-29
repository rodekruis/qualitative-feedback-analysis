"""Create llm_calls table.

Revision ID: 20260429_01
Revises:
Create Date: 2026-04-29

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260429_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the llm_calls table with full final schema."""
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_duration_ms", sa.Integer, nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_class", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint("status IN ('ok', 'error')", name="llm_calls_status_known"),
        sa.CheckConstraint("input_tokens >= 0", name="llm_calls_input_tokens_nonneg"),
        sa.CheckConstraint("output_tokens >= 0", name="llm_calls_output_tokens_nonneg"),
        sa.CheckConstraint("cost_usd >= 0", name="llm_calls_cost_nonneg"),
        sa.CheckConstraint("call_duration_ms >= 0", name="llm_calls_duration_nonneg"),
        sa.CheckConstraint(
            "(status = 'error') = (error_class IS NOT NULL)",
            name="llm_calls_error_class_iff_error",
        ),
    )
    op.create_index(
        "idx_llm_calls_tenant_timestamp",
        "llm_calls",
        ["tenant_id", "timestamp"],
    )
    op.create_index(
        "idx_llm_calls_timestamp",
        "llm_calls",
        ["timestamp"],
    )


def downgrade() -> None:
    """Drop the llm_calls table (cascades indexes and check constraints)."""
    op.drop_table("llm_calls")
