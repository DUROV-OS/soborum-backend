"""editable task report comment: updated_at (0077)

Revision ID: e5a9c247b108
Revises: d1f6b03c9a74
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e5a9c247b108'
down_revision: Union[str, None] = 'd1f6b03c9a74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL = комментарий не правили после отправки.
    op.add_column("task_reports", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("task_reports", "updated_at")
