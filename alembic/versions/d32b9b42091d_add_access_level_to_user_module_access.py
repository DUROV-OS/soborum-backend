"""add AccessLevel and level column on user_module_access (0052-a)

Revision ID: d32b9b42091d
Revises: c3e7b1a94f52
Create Date: 2026-09-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd32b9b42091d'
down_revision: Union[str, None] = 'c3e7b1a94f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    access_level = sa.Enum('NONE', 'VIEW', 'EDIT', 'FULL', name='access_level')
    access_level.create(op.get_bind(), checkfirst=True)
    op.add_column('user_module_access', sa.Column('level', access_level, nullable=True))
    # Сегодняшний грант означал полный доступ к разделу без разбора операций —
    # бэкфилл на FULL не регрессия (см. спецификацию 0052-a, п.3).
    op.execute("UPDATE user_module_access SET level = 'FULL'")
    op.alter_column('user_module_access', 'level', nullable=False)


def downgrade() -> None:
    op.drop_column('user_module_access', 'level')
    sa.Enum(name='access_level').drop(op.get_bind(), checkfirst=True)
