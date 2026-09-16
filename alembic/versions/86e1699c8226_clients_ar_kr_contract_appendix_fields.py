"""clients ar kr contract appendix fields

Revision ID: 86e1699c8226
Revises: da1142744535
Create Date: 2026-09-16 17:06:29.029106

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '86e1699c8226'
down_revision: Union[str, None] = 'da1142744535'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0061: АР/КР обязательны, приложение к договору грузится вместе с
    # договором, проект дома становится необязательным (гейт живёт в коде,
    # не в БД — тут только новые колонки).
    op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'CONTRACT_APPENDIX'")
    op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'ARCHITECTURAL_DECISIONS'")
    op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'CONSTRUCTIVE_DECISIONS'")

    op.add_column('clients', sa.Column('contract_appendix_file_id', sa.Integer(), nullable=True))
    op.add_column('clients', sa.Column('ar_file_id', sa.Integer(), nullable=True))
    op.add_column('clients', sa.Column('kr_file_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_clients_contract_appendix_file_id_file_assets',
        'clients', 'file_assets', ['contract_appendix_file_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_clients_ar_file_id_file_assets',
        'clients', 'file_assets', ['ar_file_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_clients_kr_file_id_file_assets',
        'clients', 'file_assets', ['kr_file_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_clients_kr_file_id_file_assets', 'clients', type_='foreignkey')
    op.drop_constraint('fk_clients_ar_file_id_file_assets', 'clients', type_='foreignkey')
    op.drop_constraint('fk_clients_contract_appendix_file_id_file_assets', 'clients', type_='foreignkey')
    op.drop_column('clients', 'kr_file_id')
    op.drop_column('clients', 'ar_file_id')
    op.drop_column('clients', 'contract_appendix_file_id')
    # Postgres has no DROP VALUE for enums — new file_purpose values from
    # upgrade() are intentionally left in place.
