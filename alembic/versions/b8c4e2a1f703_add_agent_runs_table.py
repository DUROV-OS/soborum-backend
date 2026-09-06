"""agent_runs: live coordinator traces for the Agents panel

Revision ID: b8c4e2a1f703
Revises: a7f3c1e9d204
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8c4e2a1f703"
down_revision: Union[str, None] = "a7f3c1e9d204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("legal_verdict", sa.String(length=32), nullable=False),
        sa.Column("legal_rules", sa.JSON(), nullable=False),
        sa.Column("legal_passport", sa.Text(), nullable=False),
        sa.Column("released", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("specialists", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_trace_id", "agent_runs", ["trace_id"], unique=True)
    op.create_index("ix_agent_runs_legal_verdict", "agent_runs", ["legal_verdict"])
    op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_legal_verdict", table_name="agent_runs")
    op.drop_index("ix_agent_runs_trace_id", table_name="agent_runs")
    op.drop_table("agent_runs")
