"""Create llm_calls table.

Revision ID: fbeeb04c3d36
Revises:
Create Date: 2026-03-11 15:03:31.348745

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fbeeb04c3d36"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create llm_calls table."""
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_duration_ms", sa.Integer, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
    )
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])


def downgrade() -> None:
    """Drop llm_calls table."""
    op.drop_index("ix_llm_calls_tenant_id", table_name="llm_calls")
    op.drop_table("llm_calls")
