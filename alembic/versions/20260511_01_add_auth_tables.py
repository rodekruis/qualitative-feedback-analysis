"""Add tenants and keys tables.

Revision ID: 20260511_01
Revises: 20260429_01
Create Date: 2026-05-11

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511_01"
down_revision: Union[str, Sequence[str], None] = "20260429_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create auth tables for tenants and API keys."""
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "allows_superusers",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "keys",
        sa.Column("key_id", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hashed_key", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column(
            "is_superuser", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
    )

    op.create_index("idx_keys_tenant_id", "keys", ["tenant_id"])
    op.create_index("idx_keys_hashed_key", "keys", ["hashed_key"])


def downgrade() -> None:
    """Drop auth tables (keys first because of FK)."""
    op.drop_index("idx_keys_hashed_key", table_name="keys")
    op.drop_index("idx_keys_tenant_id", table_name="keys")
    op.drop_table("keys")
    op.drop_table("tenants")
