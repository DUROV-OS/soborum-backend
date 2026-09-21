"""house_model_cards typical_ar_file_id/typical_kr_file_id (0073-b)

Revision ID: 476439581e96
Revises: 7647843c5a8a
Create Date: 2026-09-20 00:00:01.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '476439581e96'
down_revision: Union[str, None] = '7647843c5a8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Единственное исключение из read-only витрины (0073-b): типовые АР/КР
    # самой модели, правятся только через PATCH /catalog/{key}/typical-documents
    # (require_admin) — не участвуют в генерации графа этапов клиента.
    op.add_column('house_model_cards', sa.Column('typical_ar_file_id', sa.Integer(), nullable=True))
    op.add_column('house_model_cards', sa.Column('typical_kr_file_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_house_model_cards_typical_ar_file_id_file_assets',
        'house_model_cards', 'file_assets', ['typical_ar_file_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_house_model_cards_typical_kr_file_id_file_assets',
        'house_model_cards', 'file_assets', ['typical_kr_file_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_house_model_cards_typical_kr_file_id_file_assets', 'house_model_cards', type_='foreignkey'
    )
    op.drop_constraint(
        'fk_house_model_cards_typical_ar_file_id_file_assets', 'house_model_cards', type_='foreignkey'
    )
    op.drop_column('house_model_cards', 'typical_kr_file_id')
    op.drop_column('house_model_cards', 'typical_ar_file_id')
