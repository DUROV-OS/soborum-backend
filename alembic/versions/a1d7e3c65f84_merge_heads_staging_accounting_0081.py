"""merge heads on staging: бухгалтерия 0081 + сведение голов стейджа

Revision ID: a1d7e3c65f84
Revises: c8b5d1f47a29, d5e2a90c1b77
Create Date: 2026-09-24 00:00:00.000000

Только для ветки staging. Цепочка задачи 0081 (c3b8f1a06d42 -> d5e2a90c1b77)
отрезана от f1c6d3b78a25 — так она уйдёт в main одной головой. На стейдже от
той же ревизии уже растёт сведение c8b5d1f47a29, отсюда две головы. Своих
изменений схемы миграция не несёт.
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = 'a1d7e3c65f84'
down_revision: Union[str, Sequence[str], None] = ('c8b5d1f47a29', 'd5e2a90c1b77')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
