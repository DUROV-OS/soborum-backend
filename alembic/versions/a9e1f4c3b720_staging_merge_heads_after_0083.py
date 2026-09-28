"""свести головы alembic на стейдже после 0083

Только для ветки staging: у стейджа своя голова f3a9c2d81e45 (ветки, которых
нет в main, + 0082-a), а 0083 пришла цепочкой от main
(a3f7c2e91b04 → b8e4d1f63a27 → c4d9e2a7f150). Без этой миграции у стейджа две
головы и бэкенд не поднимается.

Revision ID: a9e1f4c3b720
Revises: f3a9c2d81e45, c4d9e2a7f150
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'a9e1f4c3b720'
down_revision: Union[str, Sequence[str], None] = ('f3a9c2d81e45', 'c4d9e2a7f150')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
