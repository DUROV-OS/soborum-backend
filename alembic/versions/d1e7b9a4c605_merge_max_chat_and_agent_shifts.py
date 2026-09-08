"""merge: client max_chat_id + agent shifts (две ветки миграций сходятся)

Revision ID: d1e7b9a4c605
Revises: f5a2c8e1d7b3, c3d8e1b4a902
Create Date: 2026-09-08 00:00:00.000000

Пустой merge-ревижн: ветка payment-variety (…→ c9a1e5b73f28 → f5a2c8e1d7b3)
и ветка agents (…→ b8c4e2a1f703 → c3d8e1b4a902) обе отходят от
a7f3c1e9d204. Схему не трогает, только сводит heads в один.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "d1e7b9a4c605"
down_revision: Union[str, Sequence[str], None] = ("f5a2c8e1d7b3", "c3d8e1b4a902")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
