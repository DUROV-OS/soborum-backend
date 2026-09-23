"""task completion reports (0077)

Revision ID: c4d81f7a2e55
Revises: 2d83291bce19
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4d81f7a2e55'
down_revision: Union[str, None] = '2d83291bce19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Новое значение существующего pg-типа file_purpose — autogenerate такие
    # добавления не видит, тот же приём, что в a7c1e4b95d30.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'TASK_REPORT_FILE'")

    op.create_table(
        "task_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_reports_task_id"), "task_reports", ["task_id"])

    op.create_table(
        "task_report_files",
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["task_reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["file_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("report_id", "file_id"),
    )


def downgrade() -> None:
    op.drop_table("task_report_files")
    op.drop_index(op.f("ix_task_reports_task_id"), table_name="task_reports")
    op.drop_table("task_reports")
    # Значение TASK_REPORT_FILE в enum file_purpose остаётся: в Postgres нет
    # DROP VALUE (тот же компромисс, что в a7c1e4b95d30).
