"""merge heads on staging: клиенты 0079 + сверка стейджа с main

Revision ID: c8b5d1f47a29
Revises: a4f2c7e9b103, f1c6d3b78a25
Create Date: 2026-09-23 00:00:00.000000

Только для ветки staging. Цепочка задачи 0079 отрезана от b4d7c9e21a58 (так
она уйдёт в main), а на стейдже от той же ревизии растёт сведение голов
a4f2c7e9b103 — отсюда две головы. Своих изменений схемы миграция не несёт.
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = 'c8b5d1f47a29'
down_revision: Union[str, Sequence[str], None] = ('a4f2c7e9b103', 'f1c6d3b78a25')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
