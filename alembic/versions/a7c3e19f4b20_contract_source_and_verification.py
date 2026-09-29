"""источник и отметка проверки договора и приложения (0084-i)

Revision ID: a7c3e19f4b20
Revises: 5115058de9f9
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7c3e19f4b20'
# Миграции 0084 выстроены цепочкой в порядке мержа: эта идёт после 0084-g.
down_revision: Union[str, None] = '5115058de9f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DOCUMENTS = ("contract", "contract_appendix")


def upgrade() -> None:
    # Тип создаём явно, а в add_column передаём его с create_type=False (тот же
    # приём, что в d5e2a90c1b77): иначе SQLAlchemy попытается создать его
    # второй раз для второй колонки.
    sa.Enum("UPLOADED", "GENERATED", name="contract_source").create(op.get_bind(), checkfirst=True)
    contract_source = postgresql.ENUM("UPLOADED", "GENERATED", name="contract_source", create_type=False)

    for doc in _DOCUMENTS:
        op.add_column("clients", sa.Column(f"{doc}_source", contract_source, nullable=True))
        op.add_column(
            "clients",
            sa.Column(
                f"{doc}_verification_required", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
        )
        op.add_column("clients", sa.Column(f"{doc}_verified_by_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_clients_{doc}_verified_by_id", "clients", "users", [f"{doc}_verified_by_id"], ["id"]
        )
        op.add_column("clients", sa.Column(f"{doc}_verified_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("clients", sa.Column(f"{doc}_verification_note", sa.Text(), nullable=True))
        # Откуда взялись уже приложенные файлы, задним числом не установить —
        # считаем их загруженными. Отметки проверки у них нет, а
        # verification_required остаётся false: гейт для них — предупреждение.
        op.execute(
            f"UPDATE clients SET {doc}_source = 'UPLOADED' WHERE {doc}_file_id IS NOT NULL"
        )


def downgrade() -> None:
    for doc in reversed(_DOCUMENTS):
        op.drop_column("clients", f"{doc}_verification_note")
        op.drop_column("clients", f"{doc}_verified_at")
        op.drop_constraint(f"fk_clients_{doc}_verified_by_id", "clients", type_="foreignkey")
        op.drop_column("clients", f"{doc}_verified_by_id")
        op.drop_column("clients", f"{doc}_verification_required")
        op.drop_column("clients", f"{doc}_source")
    sa.Enum(name="contract_source").drop(op.get_bind(), checkfirst=True)
