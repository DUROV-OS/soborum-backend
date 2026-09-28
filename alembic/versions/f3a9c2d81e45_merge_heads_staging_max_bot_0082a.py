"""свести головы alembic на стейдже после 0082-a

Только для ветки staging: на стейдже уже влиты ветки, которых нет в main
(головы a1d7e3c65f84), а 0082-a отрезана от main — без этой миграции у
стейджа две головы и бэкенд не поднимается.

Revision ID: f3a9c2d81e45
Revises: a1d7e3c65f84, e7c41b9a2f60
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'f3a9c2d81e45'
down_revision: Union[str, Sequence[str], None] = ('a1d7e3c65f84', 'e7c41b9a2f60')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
