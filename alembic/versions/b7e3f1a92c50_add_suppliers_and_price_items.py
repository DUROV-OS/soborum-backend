"""suppliers + price items: реестр поставщиков, контакты, чат MAX, прайс-лист

Задача 0011-a (бывшая 0005). Единая сущность Supplier для фичи 0011 «Бухгалтерия».

Revision ID: b7e3f1a92c50
Revises: a3e7b1c5d208
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e3f1a92c50'
down_revision: Union[str, None] = 'a3e7b1c5d208'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # На PostgreSQL создаём enum-тип явно через raw SQL и передаём колонке
    # ENUM(create_type=False) — так SQLAlchemy не пытается выпустить CREATE TYPE
    # повторно во время create_table. DROP IF EXISTS убирает возможный «хвост»
    # от прежней неудачной попытки применить эту миграцию.
    if is_pg:
        op.execute("DROP TYPE IF EXISTS supplier_status")
        op.execute("CREATE TYPE supplier_status AS ENUM ('ACTIVE', 'ARCHIVED')")
        status_col: sa.types.TypeEngine = postgresql.ENUM(
            'ACTIVE', 'ARCHIVED', name='supplier_status', create_type=False
        )
    else:
        status_col = sa.Enum('ACTIVE', 'ARCHIVED', name='supplier_status')

    op.create_table(
        'suppliers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('categories', sa.JSON(), nullable=False),
        sa.Column('status', status_col, nullable=False),
        sa.Column('contacts', sa.JSON(), nullable=False),
        # id чата MAX; один чат — не более чем у одного поставщика.
        sa.Column('max_chat_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('max_chat_id', name='uq_suppliers_max_chat_id'),
    )

    op.create_table(
        'supplier_price_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        sa.Column('material', sa.String(length=255), nullable=False),
        sa.Column('category', sa.String(length=128), nullable=True),
        # диапазоны партии: [{"min_qty", "max_qty", "price"}]; max_qty=null — «и больше»
        sa.Column('tiers', sa.JSON(), nullable=False),
        sa.Column('lead_time', sa.String(length=64), nullable=True),
        sa.Column('round', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_supplier_price_items_supplier_id', 'supplier_price_items', ['supplier_id'])


def downgrade() -> None:
    op.drop_index('ix_supplier_price_items_supplier_id', table_name='supplier_price_items')
    op.drop_table('supplier_price_items')
    op.drop_table('suppliers')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("DROP TYPE IF EXISTS supplier_status")
