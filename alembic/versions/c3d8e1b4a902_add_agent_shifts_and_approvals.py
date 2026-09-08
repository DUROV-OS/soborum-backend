"""agent shifts, shift items, human-approval queue

Revision ID: c3d8e1b4a902
Revises: b8c4e2a1f703
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8e1b4a902"
down_revision: Union[str, None] = "b8c4e2a1f703"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_shifts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("claude_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_shifts_verdict", "agent_shifts", ["verdict"])
    op.create_index("ix_agent_shifts_created_at", "agent_shifts", ["created_at"])

    op.create_table(
        "agent_shift_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("daily_question", sa.Text(), nullable=False),
        sa.Column("stance", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("legal_verdict", sa.String(length=32), nullable=False),
        sa.Column("reviews", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["shift_id"], ["agent_shifts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_shift_items_agent_id", "agent_shift_items", ["agent_id"])

    op.create_table(
        "agent_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["shift_id"], ["agent_shifts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["agent_shift_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_approvals_status", "agent_approvals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_approvals_status", table_name="agent_approvals")
    op.drop_table("agent_approvals")
    op.drop_index("ix_agent_shift_items_agent_id", table_name="agent_shift_items")
    op.drop_table("agent_shift_items")
    op.drop_index("ix_agent_shifts_created_at", table_name="agent_shifts")
    op.drop_index("ix_agent_shifts_verdict", table_name="agent_shifts")
    op.drop_table("agent_shifts")
