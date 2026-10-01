"""task_report_revisions: прежние версии отчёта о сдаче (0084-g)

Revision ID: 5115058de9f9
Revises: a0726ebb1e8d
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '5115058de9f9'
# Миграции 0084 выстроены цепочкой в порядке мержа: эта идёт после 0084-f.
down_revision: Union[str, None] = 'a0726ebb1e8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_report_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "report_id",
            sa.Integer(),
            sa.ForeignKey("task_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column(
            "edited_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "edited_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "after_acceptance", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_index(
        "ix_task_report_revisions_report_id", "task_report_revisions", ["report_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_report_revisions_report_id", table_name="task_report_revisions")
    op.drop_table("task_report_revisions")
