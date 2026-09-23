"""merge heads after staging<-main sync (0077 reports + 0078 characteristics)

Revision ID: a4f2c7e9b103
Revises: b4d7c9e21a58, e5a9c247b108
Create Date: 2026-09-23 00:00:00.000000

Только для ветки staging. Стейдж не синхронизировали с main после мержа 0077,
поэтому его цепочка (…-> 18acd0e89e98 -> b4d7c9e21a58) и цепочка отчётов о
сдаче задачи из main (…-> e5a9c247b108) разошлись двумя головами. Своих
изменений схемы миграция не несёт — только сводит головы, чтобы бэкенд на
стейдже поднимался.
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = 'a4f2c7e9b103'
down_revision: Union[str, Sequence[str], None] = ('b4d7c9e21a58', 'e5a9c247b108')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
