"""client_lead_requests и clients.lead_source: приём заявок с сайта (0111-a)

Revision ID: c7d2a8f4e19b
Revises: b6c3e9a41f08
Create Date: 2026-10-14 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d2a8f4e19b'
down_revision: Union[str, None] = 'b6c3e9a41f08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('clients', sa.Column('lead_source', sa.String(length=255), nullable=True))
    op.create_table(
        'client_lead_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('utm', sa.JSON(), nullable=True),
        sa.Column('is_repeat', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_client_lead_requests_client_id', 'client_lead_requests', ['client_id'])


def downgrade() -> None:
    op.drop_index('ix_client_lead_requests_client_id', table_name='client_lead_requests')
    op.drop_table('client_lead_requests')
    op.drop_column('clients', 'lead_source')
