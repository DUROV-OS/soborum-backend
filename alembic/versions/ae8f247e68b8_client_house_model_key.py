"""client: drop rough project fields, add house_model_key

Revision ID: ae8f247e68b8
Revises: 3f115f0f0e4e
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ae8f247e68b8'
down_revision: Union[str, None] = '3f115f0f0e4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0044: the free-text "rough project" fields (wishes/area/price/layout,
    # filled at DISCUSSION) are replaced by picking a real catalog model
    # (house_model_key) at APPROVAL, alongside order_type/contract. Data in
    # these columns is intentionally dropped, not migrated — explicit,
    # confirmed request (see backlog/PROCESS/0044-client-house-selection.md).
    op.drop_column('clients', 'wishes_description')
    op.drop_column('clients', 'estimated_price')
    op.drop_column('clients', 'house_area')
    op.drop_column('clients', 'layout_notes')
    op.drop_column('clients', 'project_locked_at')

    op.add_column('clients', sa.Column('house_model_key', sa.String(length=64), nullable=True))
    op.create_foreign_key(
        'fk_clients_house_model_key_house_model_cards',
        'clients', 'house_model_cards',
        ['house_model_key'], ['key'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_clients_house_model_key_house_model_cards', 'clients', type_='foreignkey')
    op.drop_column('clients', 'house_model_key')

    op.add_column('clients', sa.Column('project_locked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('clients', sa.Column('layout_notes', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('house_area', sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column('clients', sa.Column('estimated_price', sa.Numeric(precision=14, scale=2), nullable=True))
    op.add_column('clients', sa.Column('wishes_description', sa.Text(), nullable=True))
