"""Extend llm_calls with operation, cost_usd, status, error_class.

Revision ID: 61913ced1086
Revises: fbeeb04c3d36
Create Date: 2026-04-28 16:44:09.486908

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "61913ced1086"
down_revision: Union[str, Sequence[str], None] = "fbeeb04c3d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Extend llm_calls with operation, cost_usd, status, error_class, indexes."""
    op.add_column(
        "llm_calls",
        sa.Column(
            "operation",
            sa.String(length=64),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="ok",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "error_class",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.alter_column("llm_calls", "operation", server_default=None)
    op.alter_column("llm_calls", "cost_usd", server_default=None)
    op.alter_column("llm_calls", "status", server_default=None)

    op.create_check_constraint(
        "llm_calls_status_known",
        "llm_calls",
        "status IN ('ok', 'error')",
    )
    op.create_check_constraint(
        "llm_calls_input_tokens_nonneg",
        "llm_calls",
        "input_tokens >= 0",
    )
    op.create_check_constraint(
        "llm_calls_output_tokens_nonneg",
        "llm_calls",
        "output_tokens >= 0",
    )
    op.create_check_constraint(
        "llm_calls_cost_nonneg",
        "llm_calls",
        "cost_usd >= 0",
    )
    op.create_check_constraint(
        "llm_calls_duration_nonneg",
        "llm_calls",
        "call_duration_ms >= 0",
    )
    op.create_check_constraint(
        "llm_calls_error_class_iff_error",
        "llm_calls",
        "(status = 'error') = (error_class IS NOT NULL)",
    )

    op.drop_index("ix_llm_calls_tenant_id", table_name="llm_calls")
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
    """Reverse the upgrade."""
    op.drop_index("idx_llm_calls_timestamp", table_name="llm_calls")
    op.drop_index("idx_llm_calls_tenant_timestamp", table_name="llm_calls")
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])

    op.drop_constraint("llm_calls_error_class_iff_error", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_duration_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_cost_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_output_tokens_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_input_tokens_nonneg", "llm_calls", type_="check")
    op.drop_constraint("llm_calls_status_known", "llm_calls", type_="check")

    op.drop_column("llm_calls", "created_at")
    op.drop_column("llm_calls", "error_class")
    op.drop_column("llm_calls", "status")
    op.drop_column("llm_calls", "cost_usd")
    op.drop_column("llm_calls", "operation")
