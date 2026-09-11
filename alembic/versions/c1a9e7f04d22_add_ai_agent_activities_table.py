"""ai_agent_activities: agent activity log for the "Марина" section (0033)

Revision ID: c1a9e7f04d22
Revises: 9fce253f8073
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1a9e7f04d22"
down_revision: Union[str, None] = "9fce253f8073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_agent_activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("autonomous", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("related_section", sa.String(length=32), nullable=True),
        sa.Column("related_path", sa.String(length=255), nullable=True),
        sa.Column("related_label", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_agent_activities_created_at", "ai_agent_activities", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_agent_activities_created_at", table_name="ai_agent_activities")
    op.drop_table("ai_agent_activities")
