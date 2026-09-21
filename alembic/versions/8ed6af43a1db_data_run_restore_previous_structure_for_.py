"""data: run restore_previous_structure for board tree (0028)

Revision ID: 8ed6af43a1db
Revises: fd74087d8ab9
Create Date: 2026-09-21 16:42:05.099888

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session


# revision identifiers, used by Alembic.
revision: str = '8ed6af43a1db'
down_revision: Union[str, None] = 'fd74087d8ab9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Задача 0028: код в app/board/seed.py уже был обновлён на "старую"
    # структуру (6 направлений/27 поднаправлений), но ensure_seed() -
    # единственное, что запускается на старте приложения - специально
    # no-op, если в БД уже есть корень (см. её докстринг). На уже
    # заполненной базе (стейдж/прод) код-only деплой сам по себе ничего
    # не меняет: дерево остаётся прежним, пока кто-то явно не вызовет
    # restore_previous_structure() против живой БД. Раньше это
    # предполагался ручной серверный скрипт, но агент не имеет
    # SSH-доступа к серверу (см. 0069/README) - миграция здесь
    # гарантирует, что замена дерева реально происходит на деплое, а не
    # только в коде. Идемпотентно (можно накатить повторно без вреда, см.
    # докстринг функции) - выполняется один раз благодаря обычному
    # alembic-версионированию.
    from app.board.seed import restore_previous_structure

    session = Session(bind=op.get_bind())
    try:
        restore_previous_structure(session)
    finally:
        session.close()


def downgrade() -> None:
    # Дерево совета директоров - это данные, не схема; откатывать замену
    # структуры вниз по ревизии не нужно (то же решение, что уже принято
    # для остальных enum/data-миграций этого файла - downgrade no-op).
    pass
