"""client stages: site_visit / acceptance / completed (0079-a)

Revision ID: d2f4a8b16c93
Revises: b4d7c9e21a58
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd2f4a8b16c93'
down_revision: Union[str, None] = 'b4d7c9e21a58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLAlchemy хранит в client_stage ИМЕНА членов enum (LEAD, DISCUSSION, …),
# а не их значения — отсюда верхний регистр.
NEW_VALUES = ("SITE_VISIT", "ACCEPTANCE", "COMPLETED")


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE нельзя выполнить внутри транзакции миграции.
    with op.get_context().autocommit_block():
        for value in NEW_VALUES:
            op.execute(f"ALTER TYPE client_stage ADD VALUE IF NOT EXISTS '{value}'")

    # Разовое выравнивание уже заведённых клиентов: до 0079 путь клиента
    # обрывался на «постоплате», поэтому там стоят и те, чей дом уже принят
    # или чей цикл закрыт. Клиентов на более ранних стадиях не трогаем.
    op.execute(
        """
        UPDATE clients SET stage = 'COMPLETED'
        WHERE stage = 'POSTPAYMENT'
          AND cycle_id IN (SELECT id FROM cycles WHERE status = 'COMPLETED')
        """
    )
    op.execute(
        """
        UPDATE clients SET stage = 'ACCEPTANCE'
        WHERE stage = 'POSTPAYMENT'
          AND cycle_id IN (
              SELECT cycle_id FROM installations WHERE stage = 'FOLLOWUP'
          )
        """
    )


def downgrade() -> None:
    # Значения enum в PostgreSQL не удаляются; возвращаем клиентов новых
    # стадий на «постоплату», чтобы старый код их понимал.
    op.execute(
        "UPDATE clients SET stage = 'POSTPAYMENT' WHERE stage IN ('ACCEPTANCE', 'COMPLETED')"
    )
    op.execute("UPDATE clients SET stage = 'DISCUSSION' WHERE stage = 'SITE_VISIT'")
