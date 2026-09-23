"""kind of task report: submission / reviewer decision (0077)

Revision ID: d1f6b03c9a74
Revises: c4d81f7a2e55
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd1f6b03c9a74'
down_revision: Union[str, None] = 'c4d81f7a2e55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


kind_enum = sa.Enum(
    "SUBMISSION", "REVIEW_ACCEPTED", "REVIEW_RETURNED", name="task_report_kind"
)


def upgrade() -> None:
    kind_enum.create(op.get_bind(), checkfirst=True)
    # Все записи, созданные до этой миграции, — сдача исполнителя.
    op.add_column(
        "task_reports",
        sa.Column("kind", kind_enum, nullable=False, server_default="SUBMISSION"),
    )


def downgrade() -> None:
    op.drop_column("task_reports", "kind")
    kind_enum.drop(op.get_bind(), checkfirst=True)
