"""ai_growth_proposals: предложения по развитию бизнеса в «Марине» (0036-a)

Revision ID: da1142744535
Revises: b9a2e6775692
Create Date: 2026-09-15 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "da1142744535"
down_revision: Union[str, None] = "b9a2e6775692"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum value on the existing pg enum type - autogenerate doesn't detect
    # added values, so this is done by hand (same precedent as c1d5e9f37a84).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'GROWTH_PROPOSAL'")

    op.create_table(
        "ai_growth_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("checkable_result", sa.Text(), nullable=False),
        sa.Column("executor_and_estimate", sa.Text(), nullable=False),
        sa.Column("expected_effect", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("OPEN", "TASK_CREATED", name="ai_growth_proposal_status"),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_growth_proposals_created_at", "ai_growth_proposals", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_growth_proposals_created_at", table_name="ai_growth_proposals")
    op.drop_table("ai_growth_proposals")
    sa.Enum(name="ai_growth_proposal_status").drop(op.get_bind(), checkfirst=True)
    # PostgreSQL не умеет удалять значения из enum; 'GROWTH_PROPOSAL' на
    # task_link_type остаётся безвредно.
